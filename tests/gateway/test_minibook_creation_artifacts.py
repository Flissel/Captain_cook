from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from agenten.agent_runtime.contracts import ArtifactRef
from gateway.minibook_creation_artifacts import GatewayMinibookCreationArtifactStore
from minibook.swarm.artifact_store import FilesystemCreationArtifactStore


class _StreamingArtifactStore:
    def __init__(self, content: bytes, *, reported_size: int) -> None:
        self._content = content
        self._reported_size = reported_size
        self.read_bytes_calls = 0
        self.read_sizes: list[int] = []

    def read_bytes(self, _reference) -> bytes:
        self.read_bytes_calls += 1
        raise AssertionError("unbounded read_bytes must not be called")

    def local_path(self, _reference):
        owner = self

        class StreamingPath:
            def stat(self):
                return SimpleNamespace(st_size=owner._reported_size)

            def open(self, mode: str):
                assert mode == "rb"
                stream = BytesIO(owner._content)

                class RecordingStream:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        stream.close()

                    def read(self, size: int) -> bytes:
                        owner.read_sizes.append(size)
                        return stream.read(size)

                return RecordingStream()

        return StreamingPath()


def test_gateway_reads_exact_bytes_from_minibook_owned_creation_cas(
    tmp_path: Path,
) -> None:
    root = tmp_path / "creation-cas"
    minibook = FilesystemCreationArtifactStore(root)
    written = minibook.put(b"sealed-candidate", "application/zip", namespace="source")
    reference = ArtifactRef.model_validate(written.model_dump(mode="json"))
    gateway = GatewayMinibookCreationArtifactStore(root)

    assert gateway.read_bytes(reference) == b"sealed-candidate"
    assert gateway.read_bytes_bounded(
        reference,
        max_bytes=len(b"sealed-candidate"),
    ) == b"sealed-candidate"
    assert gateway.local_path(reference) == minibook.local_path(written)
    assert gateway.metadata(reference) == {
        "sha256": written.sha256,
        "media_type": "application/zip",
        "size_bytes": len(b"sealed-candidate"),
    }


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


def test_gateway_bounded_read_stops_without_unbounded_allocation(tmp_path: Path) -> None:
    gateway = GatewayMinibookCreationArtifactStore(tmp_path / "creation-cas")
    streaming = _StreamingArtifactStore(b"x" * 100_000, reported_size=10)
    gateway._store = streaming
    reference = ArtifactRef(
        uri="artifact://minibook-creation/evidence/" + "a" * 64,
        sha256="a" * 64,
        media_type="application/json",
    )

    with pytest.raises(ValueError, match="size"):
        gateway.read_bytes_bounded(reference, max_bytes=10)

    assert streaming.read_bytes_calls == 0
    assert streaming.read_sizes == [11]


def test_gateway_bounded_read_rejects_digest_mismatch(tmp_path: Path) -> None:
    gateway = GatewayMinibookCreationArtifactStore(tmp_path / "creation-cas")
    streaming = _StreamingArtifactStore(b"tampered", reported_size=8)
    gateway._store = streaming
    expected = hashlib.sha256(b"expected").hexdigest()
    reference = ArtifactRef(
        uri=f"artifact://minibook-creation/evidence/{expected}",
        sha256=expected,
        media_type="application/json",
    )

    with pytest.raises(ValueError, match="digest"):
        gateway.read_bytes_bounded(reference, max_bytes=32)

    assert streaming.read_bytes_calls == 0
