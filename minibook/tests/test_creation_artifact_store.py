from __future__ import annotations

from pathlib import Path

import pytest

from minibook.swarm.artifact_store import (
    CreationArtifactStoreError,
    FilesystemCreationArtifactStore,
)


def test_creation_artifact_store_round_trips_digest_bound_bytes(tmp_path: Path) -> None:
    store = FilesystemCreationArtifactStore(tmp_path / "cas")

    first = store.put(b"candidate-bytes", "application/zip", namespace="source")
    replay = store.put(b"candidate-bytes", "application/zip", namespace="source")

    assert first == replay
    assert first.uri.endswith(first.sha256)
    assert store.read_bytes(first) == b"candidate-bytes"


def test_creation_artifact_store_detects_content_tampering(tmp_path: Path) -> None:
    store = FilesystemCreationArtifactStore(tmp_path / "cas")
    reference = store.put(b"sealed", "application/json", namespace="evidence")
    store.local_path(reference).write_bytes(b"changed")

    with pytest.raises(CreationArtifactStoreError, match="digest"):
        store.read_bytes(reference)


@pytest.mark.parametrize("namespace", ("", "../escape", "has/slash", "UPPER"))
def test_creation_artifact_store_rejects_unsafe_namespaces(
    tmp_path: Path,
    namespace: str,
) -> None:
    store = FilesystemCreationArtifactStore(tmp_path / "cas")

    with pytest.raises(ValueError, match="namespace"):
        store.put(b"content", "application/json", namespace=namespace)
