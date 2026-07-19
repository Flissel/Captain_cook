"""Durable, content-addressed Hermes transcript evidence for factory blocks."""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Protocol

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
