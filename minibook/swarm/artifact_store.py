"""Small immutable filesystem CAS owned by Minibook creation workers."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import tempfile

from .contracts import ArtifactRef


_NAMESPACE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_MEDIA_TYPE = re.compile(r"^[a-z0-9.+-]+/[a-z0-9.+-]+$")
_URI_PREFIX = "artifact://minibook-creation/"


class CreationArtifactStoreError(RuntimeError):
    pass


class FilesystemCreationArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, content: bytes, media_type: str, *, namespace: str) -> ArtifactRef:
        if not isinstance(content, bytes):
            raise TypeError("creation artifact content must be bytes")
        if _NAMESPACE.fullmatch(namespace) is None:
            raise ValueError("creation artifact namespace is invalid")
        if _MEDIA_TYPE.fullmatch(media_type) is None:
            raise ValueError("creation artifact media type is invalid")
        digest = hashlib.sha256(content).hexdigest()
        target = self._content_path(digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            if target.read_bytes() != content:
                raise CreationArtifactStoreError("immutable CAS digest collision")
        else:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{digest}.",
                suffix=".tmp",
                dir=target.parent,
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        return ArtifactRef(
            uri=f"{_URI_PREFIX}{namespace}/{digest}",
            sha256=digest,
            media_type=media_type,
        )

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        content = self.local_path(reference).read_bytes()
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise CreationArtifactStoreError("creation artifact digest changed")
        return content

    def local_path(self, reference: ArtifactRef) -> Path:
        if not isinstance(reference, ArtifactRef):
            raise TypeError("creation artifact reference has the wrong type")
        if not reference.uri.startswith(_URI_PREFIX):
            raise ValueError("creation artifact reference is outside the local CAS")
        suffix = reference.uri.removeprefix(_URI_PREFIX)
        parts = suffix.split("/")
        if (
            len(parts) != 2
            or _NAMESPACE.fullmatch(parts[0]) is None
            or parts[1] != reference.sha256
        ):
            raise ValueError("creation artifact reference is invalid")
        target = self._content_path(reference.sha256)
        if not target.is_file():
            raise CreationArtifactStoreError("creation artifact is unavailable")
        return target

    def _content_path(self, digest: str) -> Path:
        return self.root / "objects" / digest[:2] / digest


__all__ = ["CreationArtifactStoreError", "FilesystemCreationArtifactStore"]
