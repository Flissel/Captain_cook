"""Read-only Gateway adapter for Minibook's local creation CAS."""

from __future__ import annotations

import hashlib
from pathlib import Path

from agenten.agent_runtime.contracts import ArtifactRef
from minibook.swarm.artifact_store import (
    CreationArtifactStoreError,
    FilesystemCreationArtifactStore,
)
from minibook.swarm.contracts import ArtifactRef as MinibookArtifactRef


class GatewayMinibookCreationArtifactStore:
    """Translate refs at the composition boundary; Minibook retains CAS ownership."""

    def __init__(self, root: Path) -> None:
        self._store = FilesystemCreationArtifactStore(root)

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        try:
            return self._store.read_bytes(self._convert(reference))
        except CreationArtifactStoreError as exc:
            raise ValueError(str(exc)) from exc

    def read_bytes_bounded(
        self,
        reference: ArtifactRef,
        *,
        max_bytes: int,
    ) -> bytes:
        if (
            not isinstance(max_bytes, int)
            or isinstance(max_bytes, bool)
            or max_bytes < 1
        ):
            raise ValueError("creation artifact read limit is invalid")
        converted = self._convert(reference)
        try:
            path = self._store.local_path(converted)
            size = path.stat().st_size
            if isinstance(size, int) and size > max_bytes:
                raise ValueError("creation artifact exceeds its read size limit")
            digest = hashlib.sha256()
            content = bytearray()
            with path.open("rb") as stream:
                while len(content) <= max_bytes:
                    chunk = stream.read(
                        min(64 * 1024, max_bytes + 1 - len(content))
                    )
                    if not chunk:
                        break
                    content.extend(chunk)
                    digest.update(chunk)
                    if len(content) > max_bytes:
                        raise ValueError(
                            "creation artifact exceeds its read size limit"
                        )
        except CreationArtifactStoreError as exc:
            raise ValueError(str(exc)) from exc
        except OSError as exc:
            raise ValueError("creation artifact is unavailable") from exc
        if digest.hexdigest() != reference.sha256:
            raise ValueError("creation artifact digest changed")
        return bytes(content)

    def local_path(self, reference: ArtifactRef) -> Path:
        try:
            return self._store.local_path(self._convert(reference))
        except CreationArtifactStoreError as exc:
            raise ValueError(str(exc)) from exc

    def metadata(self, reference: ArtifactRef) -> dict[str, object]:
        """Return verified read-only metadata; never mutate Minibook's CAS."""

        content = self.read_bytes(reference)
        return {
            "sha256": reference.sha256,
            "media_type": reference.media_type,
            "size_bytes": len(content),
        }

    @staticmethod
    def _convert(reference: ArtifactRef) -> MinibookArtifactRef:
        if not isinstance(reference, ArtifactRef):
            raise TypeError("Gateway creation artifact reference has the wrong type")
        return MinibookArtifactRef.model_validate(reference.model_dump(mode="json"))


__all__ = ["GatewayMinibookCreationArtifactStore"]
