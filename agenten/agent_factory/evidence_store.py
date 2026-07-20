"""Durable, content-addressed Hermes transcript evidence for factory blocks."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Protocol
from uuid import UUID

from agenten.agent_factory.contracts import AgentFactoryJob
from agenten.agent_runtime.contracts import ArtifactRef


class FactoryEvidenceStore(Protocol):
    async def persist(self, job: AgentFactoryJob, content: bytes) -> ArtifactRef: ...


class FilesystemFactoryEvidenceStore:
    """Persist immutable Hermes output beneath a Captain-owned evidence root."""

    def __init__(self, root: Path) -> None:
        self._root = root

    async def persist(self, job: AgentFactoryJob, content: bytes) -> ArtifactRef:
        digest = hashlib.sha256(content).hexdigest()
        path = self._path_for(job, digest)
        await asyncio.to_thread(self._write_once, path, content)
        return ArtifactRef(
            uri=f"artifact://factory-evidence/{job.job_id}/{digest}",
            sha256=digest,
            media_type="application/json",
        )

    async def read(self, reference: ArtifactRef) -> bytes:
        path = self._path_from_reference(reference)
        return await asyncio.to_thread(path.read_bytes)

    async def require(self, reference: ArtifactRef) -> None:
        content = await self.read(reference)
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("factory evidence digest does not match reference")

    def _path_for(self, job: AgentFactoryJob, digest: str) -> Path:
        return self._root / str(job.job_id) / f"{digest}.json"

    def _path_from_reference(self, reference: ArtifactRef) -> Path:
        prefix = "artifact://factory-evidence/"
        if not reference.uri.startswith(prefix):
            raise ValueError("factory evidence reference is outside this store")
        parts = reference.uri.removeprefix(prefix).split("/")
        if len(parts) != 2 or parts[1] != reference.sha256:
            raise ValueError("factory evidence reference does not match digest")
        return self._root / parts[0] / f"{parts[1]}.json"

    @staticmethod
    def _write_once(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if path.read_bytes() != content:
                raise ValueError("factory evidence digest collision")
            return
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(content)
        temporary.replace(path)


class SkillEvaluationEvidenceStore(Protocol):
    """Content-addressed private evaluation record storage."""

    async def persist(
        self,
        evaluation_id: UUID,
        record_kind: str,
        record_id: str,
        content: bytes,
    ) -> ArtifactRef: ...

    async def read(self, reference: ArtifactRef) -> bytes: ...

    async def require(self, reference: ArtifactRef) -> None: ...


class FilesystemSkillEvaluationEvidenceStore:
    """Write-once private records for deterministic skill-evaluation tests."""

    _URI_PREFIX = "artifact://factory-skill-evaluations/"

    def __init__(
        self,
        root: Path = Path("artifacts/agent-factory/skill-evaluations"),
    ) -> None:
        self._root = root

    async def persist(
        self,
        evaluation_id: UUID,
        record_kind: str,
        record_id: str,
        content: bytes,
    ) -> ArtifactRef:
        self._require_segment(record_kind, "record_kind")
        self._require_segment(record_id, "record_id")
        digest = hashlib.sha256(content).hexdigest()
        path = self._path_for(evaluation_id, record_kind, record_id, digest)
        await asyncio.to_thread(FilesystemFactoryEvidenceStore._write_once, path, content)
        return ArtifactRef(
            uri=(
                f"{self._URI_PREFIX}{evaluation_id}/{record_kind}/{record_id}/{digest}"
            ),
            sha256=digest,
            media_type="application/json",
        )

    async def read(self, reference: ArtifactRef) -> bytes:
        path = self._path_from_reference(reference)
        return await asyncio.to_thread(path.read_bytes)

    async def require(self, reference: ArtifactRef) -> None:
        content = await self.read(reference)
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise ValueError("skill evaluation evidence digest does not match reference")

    def _path_for(
        self,
        evaluation_id: UUID,
        record_kind: str,
        record_id: str,
        digest: str,
    ) -> Path:
        return self._root / str(evaluation_id) / record_kind / record_id / f"{digest}.json"

    def _path_from_reference(self, reference: ArtifactRef) -> Path:
        if not reference.uri.startswith(self._URI_PREFIX):
            raise ValueError("skill evaluation reference is outside this store")
        parts = reference.uri.removeprefix(self._URI_PREFIX).split("/")
        if len(parts) != 4 or parts[3] != reference.sha256:
            raise ValueError("skill evaluation reference does not match digest")
        evaluation_id, record_kind, record_id, digest = parts
        try:
            parsed_evaluation_id = UUID(evaluation_id)
        except ValueError as exc:
            raise ValueError("skill evaluation reference contains an invalid evaluation id") from exc
        self._require_segment(record_kind, "record_kind")
        self._require_segment(record_id, "record_id")
        return self._path_for(parsed_evaluation_id, record_kind, record_id, digest)

    @staticmethod
    def _require_segment(value: str, field_name: str) -> None:
        if not value or value in {".", ".."} or "/" in value or "\\" in value:
            raise ValueError(f"{field_name} must be a safe storage segment")
