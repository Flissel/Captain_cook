"""Hermes coordinator for one durable Minibook V3 build assignment."""

from __future__ import annotations

import re
import json
import os
import subprocess
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
import httpx

from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    ToolGapMarkerV1,
    codex_build_receipt_sha256,
    validate_build_effect_binding,
)


_WORKER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SKILL_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HELD_EXECUTION_LOCKS: ContextVar[frozenset[UUID]] = ContextVar(
    "hermes_factory_held_execution_locks",
    default=frozenset(),
)


class HermesFactoryMinibookPort(Protocol):
    async def next_assignment(
        self,
        scope: "HermesFactoryAssignmentScope | None" = None,
    ) -> FactoryBuildAssignmentV2 | None: ...

    async def claim(
        self,
        assignment_id: UUID,
        worker_id: str,
    ) -> FactoryBuildAssignmentV2: ...

    async def accepted_effect(
        self,
        assignment_id: UUID,
    ) -> FactoryBuildEffectV2 | None: ...

    async def submit_effect(
        self,
        effect: FactoryBuildEffectV2,
    ) -> FactoryBuildEffectV2: ...


class HermesFactorySkillVerifierPort(Protocol):
    def validate_runtime_skill(
        self,
        *,
        skill_name: str,
        released_skill_sha256: str,
    ) -> None: ...


class HermesFactoryCodexBuild(BaseModel):
    """Typed, Captain-sealed Codex output accepted by the Hermes worker."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    hermes_usage_receipt_ref: ArtifactRef
    codex_build_receipt_ref: ArtifactRef
    codex_build_receipt: CodexBuildReceiptV1
    build_evidence_refs: tuple[ArtifactRef, ...] = Field(min_length=1)
    tool_gaps: tuple[ToolGapMarkerV1, ...] = ()

    def validate_for(self, assignment: FactoryBuildAssignmentV2) -> None:
        receipt = self.codex_build_receipt
        if receipt.assignment_id != assignment.assignment_id:
            raise ValueError("Codex build receipt assignment does not match")
        if (
            receipt.creation_job_id != assignment.creation_job_id
            or receipt.factory_job_id != assignment.factory_job_id
            or receipt.correlation_id != assignment.correlation_id
            or receipt.subject_version != assignment.subject_version
            or receipt.attempt != assignment.attempt
            or receipt.workspace_ref != assignment.workspace_ref
            or receipt.idempotency_key != assignment.effect_key
            or receipt.seal_idempotency_key != assignment.effect_key
        ):
            raise ValueError("Codex build receipt identity does not match assignment")
        expected_digest = codex_build_receipt_sha256(receipt)
        if (
            self.codex_build_receipt_ref.sha256 != expected_digest
            or self.codex_build_receipt_ref.media_type != "application/json"
            or self.codex_build_receipt_ref.uri
            != f"artifact://captain-codex-build/build-receipt/{expected_digest}"
        ):
            raise ValueError("Codex build receipt reference does not match receipt")
        if self.hermes_usage_receipt_ref.media_type != "application/json":
            raise ValueError("Hermes usage receipt must be application/json")


class HermesFactoryCodexPort(Protocol):
    async def build(
        self,
        assignment: FactoryBuildAssignmentV2,
    ) -> HermesFactoryCodexBuild: ...


class HermesFactoryCodexReceiptReaderPort(Protocol):
    def read_receipt(self, reference: ArtifactRef) -> CodexBuildReceiptV1: ...


class HermesFactoryAssignmentScope(BaseModel):
    """Identity fence for assignments produced by one live invocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    factory_job_id: UUID
    correlation_id: UUID


class HermesFactoryRetryAuthorityPort(Protocol):
    def retry_authorization(
        self,
        assignment: FactoryBuildAssignmentV2,
        checkpoint: "HermesFactoryWorkerCheckpoint",
    ) -> "HermesFactoryRetryAuthorization | None": ...


class HermesFactoryRetryAuthorization(BaseModel):
    """One-use binding from Captain retry authority to one failed revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    assignment_id: UUID
    attempt: int
    expected_revision: int = Field(ge=1)
    authorization_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HermesFactoryWorkerCheckpoint(BaseModel):
    """Sanitized durable fence around an assignment's single Codex effect."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["hermes.factory-worker-checkpoint.v1"] = Field(
        default="hermes.factory-worker-checkpoint.v1",
        alias="schema",
        serialization_alias="schema",
    )
    assignment_id: UUID
    effect_key: str
    attempt: int
    worker_id: str
    revision: int = Field(default=1, ge=1)
    state: Literal["started", "failed", "pending_effect", "effect_submitted"]
    codex_attempts: int = Field(ge=1)
    status: Literal[
        "codex_started",
        "codex_failed",
        "effect_pending",
        "effect_submitted",
    ]
    effect: FactoryBuildEffectV2 | None = None
    failure_kind: Literal[
        "timeout",
        "process_exit",
        "null_or_untyped",
        "validation_error",
        "runtime_error",
        "exception",
    ] | None = None
    failure_exit_code: int | None = Field(default=None, ge=-32768, le=32767)
    consumed_retry_authorizations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_effect_state_binding(self) -> "HermesFactoryWorkerCheckpoint":
        requires_effect = self.state in {"pending_effect", "effect_submitted"}
        if requires_effect != (self.effect is not None):
            raise ValueError("Hermes Factory checkpoint effect state is invalid")
        if (self.state == "failed") != (self.failure_kind is not None):
            raise ValueError("Hermes Factory checkpoint failure state is invalid")
        if (self.failure_kind == "process_exit") != (
            self.failure_exit_code is not None
        ):
            raise ValueError("Hermes Factory checkpoint process exit is invalid")
        return self


class FilesystemHermesFactoryWorkerCheckpointStore:
    """Write one assignment/effect fence without prompts, paths, or responses."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def load(
        self,
        assignment: FactoryBuildAssignmentV2,
    ) -> HermesFactoryWorkerCheckpoint | None:
        with self._guard(assignment.assignment_id):
            return self._load_unlocked(assignment)

    def _load_unlocked(
        self,
        assignment: FactoryBuildAssignmentV2,
    ) -> HermesFactoryWorkerCheckpoint | None:
        path = self._path(assignment.assignment_id)
        if not path.exists():
            return None
        try:
            checkpoint = HermesFactoryWorkerCheckpoint.model_validate_json(
                path.read_bytes()
            )
        except (OSError, ValueError) as exc:
            raise ValueError("Hermes Factory worker checkpoint is invalid") from exc
        if (
            checkpoint.assignment_id != assignment.assignment_id
            or checkpoint.effect_key != assignment.effect_key
            or checkpoint.attempt != assignment.attempt
        ):
            raise ValueError("Hermes Factory worker checkpoint binding changed")
        return checkpoint

    def start(
        self,
        assignment: FactoryBuildAssignmentV2,
        *,
        worker_id: str,
        retry_authorization: HermesFactoryRetryAuthorization | None,
    ) -> HermesFactoryWorkerCheckpoint:
        with self._guard(assignment.assignment_id):
            previous = self._load_unlocked(assignment)
            if previous is None:
                checkpoint = HermesFactoryWorkerCheckpoint(
                    assignment_id=assignment.assignment_id,
                    effect_key=assignment.effect_key,
                    attempt=assignment.attempt,
                    worker_id=worker_id,
                    state="started",
                    codex_attempts=1,
                    status="codex_started",
                )
                return self._create(checkpoint)
            if previous.state not in {"started", "failed"}:
                return previous
            if previous.worker_id != worker_id:
                raise ValueError("Hermes Factory checkpoint worker identity changed")
            if retry_authorization is None:
                raise ValueError("Hermes Factory Codex retry authority is required")
            if (
                retry_authorization.assignment_id != assignment.assignment_id
                or retry_authorization.attempt != assignment.attempt
                or retry_authorization.expected_revision != previous.revision
                or retry_authorization.authorization_sha256
                in previous.consumed_retry_authorizations
            ):
                raise ValueError("Hermes Factory Codex retry authority binding changed")
            if previous.codex_attempts > assignment.limits.max_retries:
                raise ValueError("Hermes Factory assignment retry limit is exhausted")
            checkpoint = previous.model_copy(
                update={
                    "state": "started",
                    "status": "codex_started",
                    "codex_attempts": previous.codex_attempts + 1,
                    "revision": previous.revision + 1,
                    "effect": None,
                    "failure_kind": None,
                    "failure_exit_code": None,
                    "consumed_retry_authorizations": (
                        *previous.consumed_retry_authorizations,
                        retry_authorization.authorization_sha256,
                    ),
                }
            )
            return self._replace(previous, checkpoint)

    def failed(
        self,
        assignment: FactoryBuildAssignmentV2,
        previous: HermesFactoryWorkerCheckpoint,
        *,
        failure_kind: Literal[
            "timeout",
            "process_exit",
            "null_or_untyped",
            "validation_error",
            "runtime_error",
            "exception",
        ],
        failure_exit_code: int | None = None,
    ) -> HermesFactoryWorkerCheckpoint:
        failed = previous.model_copy(
            update={
                "state": "failed",
                "status": "codex_failed",
                "effect": None,
                "failure_kind": failure_kind,
                "failure_exit_code": failure_exit_code,
                "revision": previous.revision + 1,
            }
        )
        with self._guard(assignment.assignment_id):
            return self._replace(previous, failed)

    def pending_effect(
        self,
        assignment: FactoryBuildAssignmentV2,
        previous: HermesFactoryWorkerCheckpoint,
        effect: FactoryBuildEffectV2,
    ) -> HermesFactoryWorkerCheckpoint:
        pending = previous.model_copy(
            update={
                "state": "pending_effect",
                "status": "effect_pending",
                "effect": effect,
                "revision": previous.revision + 1,
            }
        )
        with self._guard(assignment.assignment_id):
            return self._replace(previous, pending)

    def submitted(
        self,
        previous: HermesFactoryWorkerCheckpoint,
    ) -> HermesFactoryWorkerCheckpoint:
        submitted = previous.model_copy(
            update={
                "state": "effect_submitted",
                "status": "effect_submitted",
                "revision": previous.revision + 1,
            }
        )
        with self._guard(previous.assignment_id):
            return self._replace(previous, submitted)

    def _create(
        self,
        checkpoint: HermesFactoryWorkerCheckpoint,
    ) -> HermesFactoryWorkerCheckpoint:
        path = self._path(checkpoint.assignment_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        content = self._content(checkpoint)
        try:
            descriptor = os.open(
                path,
                getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise ValueError("Hermes Factory worker checkpoint creation conflicts") from exc
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return checkpoint

    def _replace(
        self,
        previous: HermesFactoryWorkerCheckpoint,
        checkpoint: HermesFactoryWorkerCheckpoint,
    ) -> HermesFactoryWorkerCheckpoint:
        path = self._path(checkpoint.assignment_id)
        if path.read_bytes() != self._content(previous):
            raise ValueError("Hermes Factory worker checkpoint changed concurrently")
        temporary = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
        try:
            descriptor = os.open(
                temporary,
                getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self._content(checkpoint))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return checkpoint

    @staticmethod
    def _content(checkpoint: HermesFactoryWorkerCheckpoint) -> bytes:
        return json.dumps(
            checkpoint.model_dump(mode="json", by_alias=True),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _path(self, assignment_id: UUID) -> Path:
        return self._root / f"{assignment_id.hex}.json"

    @contextmanager
    def execution_lease(self, assignment_id: UUID):
        with self._assignment_lock(assignment_id, blocking=False):
            held = _HELD_EXECUTION_LOCKS.get()
            token = _HELD_EXECUTION_LOCKS.set(held | {assignment_id})
            try:
                yield
            finally:
                _HELD_EXECUTION_LOCKS.reset(token)

    @contextmanager
    def _guard(self, assignment_id: UUID):
        if assignment_id in _HELD_EXECUTION_LOCKS.get():
            yield
            return
        with self._assignment_lock(assignment_id, blocking=True):
            yield

    @contextmanager
    def _assignment_lock(self, assignment_id: UUID, *, blocking: bool):
        self._root.mkdir(parents=True, exist_ok=True)
        path = self._root / f"{assignment_id.hex}.lock"
        with path.open("a+b") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
                os.fsync(stream.fileno())
            stream.seek(0)
            if os.name == "nt":
                import msvcrt

                try:
                    msvcrt.locking(
                        stream.fileno(),
                        msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK,
                        1,
                    )
                except OSError as exc:
                    raise ValueError(
                        "Hermes Factory assignment execution lease is active"
                    ) from exc
                try:
                    yield
                finally:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                try:
                    fcntl.flock(
                        stream.fileno(),
                        fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB),
                    )
                except BlockingIOError as exc:
                    raise ValueError(
                        "Hermes Factory assignment execution lease is active"
                    ) from exc
                try:
                    yield
                finally:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class AuthenticatedMinibookV3WorkerClient:
    """Typed authenticated client for Minibook's V3 assignment boundary."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        worker_token: str,
        worker_id: str,
        checkpoint_store: FilesystemHermesFactoryWorkerCheckpointStore,
    ) -> None:
        if not worker_token.strip():
            raise ValueError("Minibook V3 worker token is required")
        if _WORKER_ID.fullmatch(worker_id) is None:
            raise ValueError("Minibook V3 worker identity is invalid")
        self._client = client
        self._authorization = f"Bearer {worker_token}"
        self._worker_id = worker_id
        self._checkpoint_store = checkpoint_store
        self._assignments: dict[UUID, FactoryBuildAssignmentV2] = {}

    async def next_assignment(
        self,
        scope: HermesFactoryAssignmentScope | None = None,
    ) -> FactoryBuildAssignmentV2 | None:
        response = await self._client.get(
            "/v3/creation/assignments/next",
            headers=self._headers(),
            params=(
                {
                    "factory_job_id": str(scope.factory_job_id),
                    "correlation_id": str(scope.correlation_id),
                }
                if scope is not None
                else None
            ),
        )
        if response.status_code == 204:
            return None
        assignment = self._assignment_response(response)
        if scope is not None and (
            assignment.factory_job_id != scope.factory_job_id
            or assignment.correlation_id != scope.correlation_id
        ):
            return None
        self._assignments[assignment.assignment_id] = assignment
        return assignment

    async def claim(
        self,
        assignment_id: UUID,
        worker_id: str,
    ) -> FactoryBuildAssignmentV2:
        if worker_id != self._worker_id:
            raise ValueError("Minibook V3 worker identity changed")
        response = await self._client.post(
            f"/v3/creation/assignments/{assignment_id}/claim",
            headers=self._headers(),
            json={"worker_id": worker_id},
        )
        assignment = self._assignment_response(response)
        if assignment.assignment_id != assignment_id:
            raise ValueError("Minibook V3 claim assignment identity changed")
        self._assignments[assignment_id] = assignment
        return assignment

    async def accepted_effect(
        self,
        assignment_id: UUID,
    ) -> FactoryBuildEffectV2 | None:
        assignment = self._assignments.get(assignment_id)
        if assignment is None:
            raise ValueError("Minibook V3 assignment was not resolved before replay")
        checkpoint = self._checkpoint_store.load(assignment)
        if checkpoint is None or checkpoint.state != "effect_submitted":
            return None
        return checkpoint.effect

    async def submit_effect(
        self,
        effect: FactoryBuildEffectV2,
    ) -> FactoryBuildEffectV2:
        response = await self._client.post(
            f"/v3/creation/assignments/{effect.assignment_id}/effect",
            headers=self._headers(),
            json=effect.model_dump(mode="json", by_alias=True),
        )
        if response.status_code not in {200, 202}:
            raise ValueError(
                f"Minibook V3 effect submission failed with status {response.status_code}"
            )
        try:
            accepted = FactoryBuildEffectV2.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise ValueError("Minibook V3 effect response is invalid") from exc
        assignment = self._assignments.get(effect.assignment_id)
        if assignment is None:
            raise ValueError("Minibook V3 effect assignment is not scoped")
        validate_build_effect_binding(assignment, accepted)
        if accepted != effect:
            raise ValueError("Minibook accepted a different build effect")
        return accepted

    def _assignment_response(
        self,
        response: httpx.Response,
    ) -> FactoryBuildAssignmentV2:
        if response.status_code != 200:
            raise ValueError(
                f"Minibook V3 assignment request failed with status {response.status_code}"
            )
        try:
            return FactoryBuildAssignmentV2.model_validate(response.json())
        except (ValueError, TypeError) as exc:
            raise ValueError("Minibook V3 assignment response is invalid") from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": self._authorization}


class HermesFactoryWorker:
    """Claim once, invoke Codex once, and submit one immutable build effect."""

    def __init__(
        self,
        *,
        minibook: HermesFactoryMinibookPort,
        codex: HermesFactoryCodexPort,
        skill_verifier: HermesFactorySkillVerifierPort,
        worker_id: str,
        released_skill_name: str,
        released_skill_sha256: str,
        checkpoint_store: FilesystemHermesFactoryWorkerCheckpointStore,
        codex_receipts: HermesFactoryCodexReceiptReaderPort,
        retry_authority: HermesFactoryRetryAuthorityPort | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if _WORKER_ID.fullmatch(worker_id) is None:
            raise ValueError("Hermes Factory worker identity is invalid")
        if _SKILL_NAME.fullmatch(released_skill_name) is None:
            raise ValueError("Hermes Factory released skill name is invalid")
        if _SHA256.fullmatch(released_skill_sha256) is None:
            raise ValueError("Hermes Factory released skill digest is invalid")
        self._minibook = minibook
        self._codex = codex
        self._skill_verifier = skill_verifier
        self._worker_id = worker_id
        self._released_skill_name = released_skill_name
        self._released_skill_sha256 = released_skill_sha256
        self._checkpoint_store = checkpoint_store
        self._codex_receipts = codex_receipts
        self._retry_authority = retry_authority
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def run_once(
        self,
        scope: HermesFactoryAssignmentScope | None = None,
    ) -> FactoryBuildEffectV2 | None:
        assignment = (
            await self._minibook.next_assignment()
            if scope is None
            else await self._minibook.next_assignment(scope)
        )
        if assignment is None:
            return None
        if scope is not None and (
            assignment.factory_job_id != scope.factory_job_id
            or assignment.correlation_id != scope.correlation_id
        ):
            return None
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timezone.utc.utcoffset(now):
            raise ValueError("Hermes Factory clock must be UTC")
        self._skill_verifier.validate_runtime_skill(
            skill_name=self._released_skill_name,
            released_skill_sha256=self._released_skill_sha256,
        )
        claimed = await self._minibook.claim(
            assignment.assignment_id,
            self._worker_id,
        )
        if claimed != assignment:
            raise ValueError("Minibook assignment claim changed immutable content")
        with self._checkpoint_store.execution_lease(assignment.assignment_id):
            replay = await self._minibook.accepted_effect(assignment.assignment_id)
            if replay is not None:
                validate_build_effect_binding(assignment, replay)
                return replay
            if now >= assignment.deadline_at:
                raise ValueError("Hermes Factory assignment deadline expired")

            checkpoint = self._checkpoint_store.load(assignment)
            if checkpoint is not None and checkpoint.effect is not None:
                effect = checkpoint.effect
            else:
                retry_authorization = (
                    self._retry_authority.retry_authorization(
                        assignment,
                        checkpoint,
                    )
                    if checkpoint is not None
                    and checkpoint.state in {"failed", "started"}
                    and self._retry_authority is not None
                    else None
                )
                checkpoint = self._checkpoint_store.start(
                    assignment,
                    worker_id=self._worker_id,
                    retry_authorization=retry_authorization,
                )

                try:
                    build = await self._codex.build(assignment)
                    if not isinstance(build, HermesFactoryCodexBuild):
                        self._checkpoint_store.failed(
                            assignment,
                            checkpoint,
                            failure_kind="null_or_untyped",
                        )
                        raise ValueError(
                            "Hermes Factory requires a typed Codex build result"
                        )
                    build.validate_for(assignment)
                    readback = self._codex_receipts.read_receipt(
                        build.codex_build_receipt_ref
                    )
                    if readback != build.codex_build_receipt:
                        raise ValueError("Codex build receipt CAS readback changed")
                except Exception as exc:
                    current = self._checkpoint_store.load(assignment)
                    if current is not None and current.state != "failed":
                        if isinstance(exc, TimeoutError):
                            failure_kind = "timeout"
                            exit_code = None
                        elif isinstance(exc, subprocess.CalledProcessError):
                            failure_kind = "process_exit"
                            exit_code = max(-32768, min(32767, int(exc.returncode)))
                        elif isinstance(exc, ValueError):
                            failure_kind = "validation_error"
                            exit_code = None
                        elif isinstance(exc, RuntimeError):
                            failure_kind = "runtime_error"
                            exit_code = None
                        else:
                            failure_kind = "exception"
                            exit_code = None
                        self._checkpoint_store.failed(
                            assignment,
                            checkpoint,
                            failure_kind=failure_kind,
                            failure_exit_code=exit_code,
                        )
                    raise
                receipt = build.codex_build_receipt
                effect = FactoryBuildEffectV2(
                    assignment_id=assignment.assignment_id,
                    creation_job_id=assignment.creation_job_id,
                    factory_job_id=assignment.factory_job_id,
                    correlation_id=assignment.correlation_id,
                    subject_version=assignment.subject_version,
                    checkpoint_version=assignment.checkpoint_version,
                    attempt=assignment.attempt,
                    effect_key=assignment.effect_key,
                    hermes_usage_receipt_ref=build.hermes_usage_receipt_ref,
                    codex_build_receipt_ref=build.codex_build_receipt_ref,
                    codex_session_ref=receipt.codex_session_ref,
                    workspace_snapshot_ref=receipt.workspace_snapshot_ref,
                    source_archive_ref=receipt.source_archive_ref,
                    preliminary_team_manifest_ref=receipt.candidate_manifest_ref,
                    build_evidence_refs=build.build_evidence_refs,
                    test_evidence_refs=receipt.test_evidence_refs,
                    tool_gaps=build.tool_gaps,
                )
                checkpoint = self._checkpoint_store.pending_effect(
                    assignment,
                    checkpoint,
                    effect,
                )
        accepted = await self._minibook.submit_effect(effect)
        if accepted != effect:
            raise ValueError("Minibook accepted a different build effect")
        self._checkpoint_store.submitted(checkpoint)
        return accepted


__all__ = [
    "AuthenticatedMinibookV3WorkerClient",
    "HermesFactoryCodexBuild",
    "HermesFactoryAssignmentScope",
    "HermesFactoryRetryAuthorization",
    "FilesystemHermesFactoryWorkerCheckpointStore",
    "HermesFactoryCodexPort",
    "HermesFactoryMinibookPort",
    "HermesFactorySkillVerifierPort",
    "HermesFactoryWorker",
    "HermesFactoryWorkerCheckpoint",
]
