"""Read-only Gateway adapter for Minibook's local creation CAS."""

from __future__ import annotations

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
