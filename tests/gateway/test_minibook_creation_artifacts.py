from __future__ import annotations

from pathlib import Path

import pytest

from agenten.agent_runtime.contracts import ArtifactRef
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore
from minibook.swarm.artifact_store import FilesystemCreationArtifactStore


def test_gateway_reads_exact_bytes_from_minibook_owned_creation_cas(
    tmp_path: Path,
) -> None:
    root = tmp_path / "creation-cas"
    minibook = FilesystemCreationArtifactStore(root)
    written = minibook.put(b"sealed-candidate", "application/zip", namespace="source")
    reference = ArtifactRef.model_validate(written.model_dump(mode="json"))
    gateway = GatewayMinibookCreationArtifactStore(root)

    assert gateway.read_bytes(reference) == b"sealed-candidate"
    assert gateway.local_path(reference) == minibook.local_path(written)


def test_gateway_rejects_foreign_or_tampered_minibook_creation_ref(
    tmp_path: Path,
) -> None:
    root = tmp_path / "creation-cas"
    minibook = FilesystemCreationArtifactStore(root)
    written = minibook.put(b"sealed", "application/json", namespace="evidence")
    gateway = GatewayMinibookCreationArtifactStore(root)
    foreign = ArtifactRef(
        uri="artifact://foreign/evidence/" + written.sha256,
        sha256=written.sha256,
        media_type=written.media_type,
    )

    with pytest.raises(ValueError, match="outside"):
        gateway.read_bytes(foreign)

    minibook.local_path(written).write_bytes(b"changed")
    reference = ArtifactRef.model_validate(written.model_dump(mode="json"))
    with pytest.raises(ValueError, match="digest"):
        gateway.read_bytes(reference)
