"""Captain-owned sealing of one concrete Codex build workspace.

The module deliberately has no Hermes, Minibook, or Gateway effects.  It turns
already completed Codex output into immutable, content-addressed evidence and
issues the Captain contract that downstream stages may verify.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import zlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zipfile import (
    BadZipFile,
    LargeZipFile,
    ZIP_DEFLATED,
    ZIP_STORED,
    ZipFile,
    ZipInfo,
)

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
)
from agenten.agent_factory.skill_workflow_contracts import CodexBuildBriefV1


_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
MAX_FACTORY_CODEX_SOURCE_ZIP_BYTES = 32 * 1024 * 1024
MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES = 4_096
MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES = 32 * 1024 * 1024
MAX_FACTORY_CODEX_SOURCE_ZIP_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FACTORY_CODEX_SOURCE_ZIP_MANIFEST_BYTES = 2 * 1024 * 1024
_SOURCE_ZIP_READ_CHUNK_BYTES = 64 * 1024
_SUPPORTED_SOURCE_ZIP_COMPRESSION = frozenset({ZIP_STORED, ZIP_DEFLATED})
_ZIP_DATA_DESCRIPTOR_FLAG = 0x0008
_ZIP_UTF8_FLAG = 0x0800
_ZIP_DEFLATE_OPTION_FLAGS = 0x0006
_ZIP_EOCD_SIGNATURE = 0x06054B50
_ZIP_CENTRAL_SIGNATURE = 0x02014B50
_ZIP_LOCAL_SIGNATURE = 0x04034B50
_ZIP_DATA_DESCRIPTOR_SIGNATURE = 0x08074B50
_ZIP64_EXTRA_ID = 0x0001
_ZIP_EOCD = struct.Struct("<I4H2IH")
_ZIP_CENTRAL_HEADER = struct.Struct("<I6H3I5H2I")
_ZIP_LOCAL_HEADER = struct.Struct("<I5H3I2H")
_ZIP_EXTRA_HEADER = struct.Struct("<HH")
_ZIP_DATA_DESCRIPTOR = struct.Struct("<III")
_EXCLUDED_DIRECTORIES = frozenset(
    {
        ".captain-cook",
        ".cache",
        ".git",
        ".hg",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        "__pycache__",
        "cache",
        "caches",
        "log",
        "logs",
        "node_modules",
    }
)


class CodexBuildProvenanceError(ValueError):
    """The build cannot be sealed without weakening its provenance."""


@dataclass(frozen=True, slots=True)
class _SourceZipCentralEntry:
    filename: bytes
    flags: int
    compression: int
    crc32: int
    compressed_size: int
    uncompressed_size: int
    disk_number: int
    external_attr: int
    local_header_offset: int


@dataclass(frozen=True, slots=True)
class _SourceZipLayout:
    central_directory_offset: int
    entries: tuple[_SourceZipCentralEntry, ...]


@dataclass(frozen=True, slots=True)
class _SourceZipLocalRecord:
    record_start: int
    record_end: int
    data_start: int
    data_end: int


class CodexBuildArtifactCas:
    """Small write-once filesystem CAS for private Captain build evidence."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    def reference_for(
        self,
        content: bytes,
        *,
        media_type: str,
        namespace: str,
    ) -> ArtifactRef:
        self._require_namespace(namespace)
        digest = hashlib.sha256(content).hexdigest()
        return ArtifactRef(
            uri=f"artifact://captain-codex-build/{namespace}/{digest}",
            sha256=digest,
            media_type=media_type,
        )

    def put_bytes(
        self,
        content: bytes,
        *,
        media_type: str,
        namespace: str,
    ) -> ArtifactRef:
        reference = self.reference_for(
            content,
            media_type=media_type,
            namespace=namespace,
        )
        destination = self.local_path(reference)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                destination,
                getattr(os, "O_BINARY", 0) | os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            existing = destination.read_bytes()
            if existing != content or hashlib.sha256(existing).hexdigest() != reference.sha256:
                raise CodexBuildProvenanceError(
                    "write-once CAS entry already exists with a divergent digest"
                )
            return reference
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            # A partial write remains visible on purpose: a retry then fails
            # closed instead of replacing bytes at an established CAS address.
            raise
        return reference

    def read_bytes(self, reference: ArtifactRef) -> bytes:
        path = self.local_path(reference)
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise CodexBuildProvenanceError("CAS artifact is unavailable") from exc
        if hashlib.sha256(content).hexdigest() != reference.sha256:
            raise CodexBuildProvenanceError("CAS artifact digest does not match its reference")
        return content

    def local_path(self, reference: ArtifactRef) -> Path:
        prefix = "artifact://captain-codex-build/"
        if not reference.uri.startswith(prefix):
            raise CodexBuildProvenanceError("artifact reference is not owned by Codex build CAS")
        remainder = reference.uri[len(prefix) :]
        parts = remainder.split("/")
        if len(parts) != 2:
            raise CodexBuildProvenanceError("Codex build CAS reference is malformed")
        namespace, digest = parts
        self._require_namespace(namespace)
        if digest != reference.sha256:
            raise CodexBuildProvenanceError("Codex build CAS URI digest mismatch")
        return self._root / namespace / digest

    @staticmethod
    def _require_namespace(namespace: str) -> None:
        if _NAMESPACE_PATTERN.fullmatch(namespace) is None:
            raise CodexBuildProvenanceError("CAS namespace is invalid")


class CaptainCodexBuildReceiptIssuer:
    """Seal one exact Codex build only after every authority binding validates."""

    def __init__(self, cas: CodexBuildArtifactCas) -> None:
        self._cas = cas

    def persist_receipt(self, receipt: CodexBuildReceiptV1) -> ArtifactRef:
        """Persist the canonical receipt bytes required by build evidence V1."""

        canonical = CodexBuildReceiptV1.model_validate(
            receipt.model_dump(mode="json", by_alias=True)
        )
        return self._cas.put_bytes(
            _canonical_json(canonical.model_dump(mode="json", by_alias=True)),
            media_type="application/json",
            namespace="build-receipt",
        )

    def issue(
        self,
        *,
        job: AgentFactoryJobV3,
        build_brief: CodexBuildBriefV1,
        workspace_root: Path,
        codex_session_receipt: bytes,
        seal_idempotency_key: str,
        candidate_manifest_path: str,
        source_archive_path: str,
        test_evidence_paths: Iterable[str],
        completed_at: datetime,
    ) -> CodexBuildReceiptV1:
        self._require_job_brief_binding(job, build_brief)
        assignment = build_brief.build_assignment
        root = workspace_root.resolve(strict=True)
        if not root.is_dir():
            raise CodexBuildProvenanceError("Codex workspace root must be a directory")
        if _is_relative_to(self._cas.root, root):
            raise CodexBuildProvenanceError("Codex build CAS must be outside the workspace")

        manifest_file = _resolve_workspace_file(root, candidate_manifest_path)
        source_file = _resolve_workspace_file(root, source_archive_path)
        evidence_names = tuple(test_evidence_paths)
        if not evidence_names:
            raise CodexBuildProvenanceError("at least one test evidence file is required")
        if len(evidence_names) != len(set(evidence_names)):
            raise CodexBuildProvenanceError("test evidence paths must be unique")
        evidence_files = tuple(_resolve_workspace_file(root, item) for item in evidence_names)
        # Walk the whole workspace before accepting any individual artifact.
        # This ensures an unrelated symlink cannot hide behind otherwise valid
        # manifest and source-archive bindings.
        snapshot_bytes, snapshot_files = _deterministic_workspace_zip(root)

        session_payload = _require_json_object(
            codex_session_receipt,
            label="Codex session receipt",
        )
        if "session_id" not in session_payload and "session_ref" not in session_payload:
            raise CodexBuildProvenanceError(
                "Codex session receipt must contain an opaque session binding"
            )
        manifest_bytes = snapshot_files[manifest_file.relative_to(root).as_posix()]
        manifest = _require_json_object(manifest_bytes, label="candidate manifest")
        source_bytes = snapshot_files[source_file.relative_to(root).as_posix()]
        archived_manifest_bytes = _require_safe_source_zip(source_bytes)
        if "source_archive_ref" in manifest:
            raise CodexBuildProvenanceError(
                "candidate manifest must not contain a self-referential source archive"
            )
        if archived_manifest_bytes != manifest_bytes:
            raise CodexBuildProvenanceError(
                "candidate manifest does not match the sealed source archive"
            )
        test_bytes = tuple(
            snapshot_files[path.relative_to(root).as_posix()] for path in evidence_files
        )
        for content in test_bytes:
            _require_json_object(content, label="test evidence")

        session_ref = self._cas.put_bytes(
            codex_session_receipt,
            media_type="application/json",
            namespace="codex-session",
        )
        snapshot_ref = self._cas.put_bytes(
            snapshot_bytes,
            media_type="application/zip",
            namespace="workspace-snapshot",
        )
        manifest_ref = self._cas.put_bytes(
            manifest_bytes,
            media_type="application/json",
            namespace="candidate-manifest",
        )
        source_ref = self._cas.put_bytes(
            source_bytes,
            media_type="application/zip",
            namespace="codex-source",
        )
        evidence_refs = tuple(
            self._cas.put_bytes(
                content,
                media_type="application/json",
                namespace="test-evidence",
            )
            for content in test_bytes
        )
        if len(evidence_refs) != len(set(evidence_refs)):
            raise CodexBuildProvenanceError("test evidence artifacts must be distinct")

        brief_ref = ArtifactRef.model_validate(
            build_brief.artifact_ref.model_dump(mode="json")
        )
        receipt_identity = _canonical_json(
            {
                "factory_job_id": str(job.job_id),
                "creation_job_id": str(assignment.creation_job_id),
                "assignment_id": str(assignment.assignment_id),
                "idempotency_key": assignment.idempotency_key,
                "seal_idempotency_key": seal_idempotency_key,
                "workspace_ref": assignment.workspace_ref,
                "build_brief_ref": brief_ref.model_dump(mode="json"),
                "codex_session_ref": session_ref.model_dump(mode="json"),
                "workspace_snapshot_ref": snapshot_ref.model_dump(mode="json"),
                "candidate_manifest_ref": manifest_ref.model_dump(mode="json"),
                "source_archive_ref": source_ref.model_dump(mode="json"),
                "test_evidence_refs": [item.model_dump(mode="json") for item in evidence_refs],
                "completed_at": completed_at.isoformat(),
            }
        )
        return CodexBuildReceiptV1.model_validate(
            {
                "schema": "captain.codex-build-receipt.v1",
                "receipt_id": str(uuid5(NAMESPACE_URL, receipt_identity.decode("utf-8"))),
                "producer": "captain",
                "outcome": "sealed",
                "factory_job_id": str(job.job_id),
                "creation_job_id": str(assignment.creation_job_id),
                "correlation_id": str(job.correlation_id),
                "subject_version": job.subject_version,
                "attempt": assignment.attempt,
                "assignment_id": str(assignment.assignment_id),
                "idempotency_key": assignment.idempotency_key,
                "seal_idempotency_key": seal_idempotency_key,
                "build_brief_ref": brief_ref.model_dump(mode="json"),
                "workspace_ref": assignment.workspace_ref,
                "codex_session_ref": session_ref.model_dump(mode="json"),
                "workspace_snapshot_ref": snapshot_ref.model_dump(mode="json"),
                "candidate_manifest_ref": manifest_ref.model_dump(mode="json"),
                "source_archive_ref": source_ref.model_dump(mode="json"),
                "test_evidence_refs": [
                    item.model_dump(mode="json") for item in evidence_refs
                ],
                "acceptance_assertion_ids": list(job.acceptance_assertion_ids),
                "completed_at": completed_at,
            }
        )

    @staticmethod
    def _require_job_brief_binding(
        job: AgentFactoryJobV3,
        brief: CodexBuildBriefV1,
    ) -> None:
        assignment = brief.build_assignment
        if (
            brief.job_id != job.job_id
            or brief.correlation_id != job.correlation_id
            or brief.subject_version != job.subject_version
            or tuple(brief.acceptance_assertion_ids) != tuple(job.acceptance_assertion_ids)
            or assignment.correlation_id != job.correlation_id
            or assignment.subject_version != job.subject_version
            or tuple(assignment.public_assertion_ids) != tuple(job.acceptance_assertion_ids)
            or assignment.compiled_spec_ref.model_dump(mode="json")
            != job.compiled_spec_ref.model_dump(mode="json")
            or assignment.dependency_graph_ref.model_dump(mode="json")
            != job.dependency_graph_ref.model_dump(mode="json")
        ):
            raise CodexBuildProvenanceError("factory job does not match the Codex build brief")
        if assignment.attempt != brief.attempt:
            raise CodexBuildProvenanceError(
                "assignment attempt does not match the Codex build brief"
            )
        if tuple(brief.authorized_path_roots) != (assignment.workspace_ref,):
            raise CodexBuildProvenanceError(
                "Codex build brief must authorize exactly the assigned workspace"
            )


def _resolve_workspace_file(root: Path, relative_path: str) -> Path:
    normalized = relative_path.replace("\\", "/")
    pure = PurePosixPath(normalized)
    if (
        not normalized
        or pure.is_absolute()
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
        or ".." in pure.parts
    ):
        raise CodexBuildProvenanceError("artifact path must be relative to the workspace")
    if _is_excluded(pure, is_directory=False):
        raise CodexBuildProvenanceError(
            "workspace artifact path is forbidden from the sealed snapshot"
        )
    candidate = root.joinpath(*pure.parts)
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise CodexBuildProvenanceError("workspace artifact is unavailable") from exc
    if not _is_relative_to(resolved, root) or not resolved.is_file():
        raise CodexBuildProvenanceError("artifact path must be relative to the workspace")
    if candidate.is_symlink():
        raise CodexBuildProvenanceError("workspace artifact must not be a symbolic link")
    return resolved


def _deterministic_workspace_zip(root: Path) -> tuple[bytes, dict[str, bytes]]:
    files = _collect_workspace_files(root)
    contents = {relative_path: source.read_bytes() for relative_path, source in files}
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        for relative_path, _source in files:
            info = ZipInfo(relative_path, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, contents[relative_path])
    return output.getvalue(), contents


def _collect_workspace_files(root: Path) -> tuple[tuple[str, Path], ...]:
    collected: list[tuple[str, Path]] = []

    def visit(directory: Path, prefix: PurePosixPath) -> None:
        with os.scandir(directory) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
        for entry in ordered:
            relative = prefix / entry.name
            if _is_excluded(relative, is_directory=entry.is_dir(follow_symlinks=False)):
                continue
            if entry.is_symlink():
                raise CodexBuildProvenanceError(
                    f"workspace contains a symbolic link: {relative.as_posix()}"
                )
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                visit(path, relative)
            elif entry.is_file(follow_symlinks=False):
                collected.append((relative.as_posix(), path))
            else:
                raise CodexBuildProvenanceError(
                    f"workspace contains an unsupported file type: {relative.as_posix()}"
                )

    visit(root, PurePosixPath())
    collected.sort(key=lambda item: item[0])
    folded = tuple(name.casefold() for name, _ in collected)
    if len(folded) != len(set(folded)):
        raise CodexBuildProvenanceError("workspace contains case-colliding paths")
    return tuple(collected)


def _is_excluded(path: PurePosixPath, *, is_directory: bool) -> bool:
    lowered = tuple(part.casefold() for part in path.parts)
    if any(part in _EXCLUDED_DIRECTORIES for part in lowered):
        return True
    name = lowered[-1]
    if name == ".env" or name.startswith(".env."):
        return True
    if not is_directory and (name.endswith(".log") or name.endswith((".pyc", ".pyo"))):
        return True
    return False


def _require_safe_source_zip(content: bytes) -> bytes:
    try:
        layout = _preflight_source_zip(content)
        with ZipFile(BytesIO(content)) as archive:
            items = archive.infolist()
            if len(items) != len(layout.entries):
                raise CodexBuildProvenanceError(
                    "source archive central directory count is inconsistent"
                )
            names: set[str] = set()
            validated: list[
                tuple[ZipInfo, str, _SourceZipCentralEntry, _SourceZipLocalRecord]
            ] = []
            occupied_ranges: list[tuple[int, int]] = []
            candidate_manifest_info: ZipInfo | None = None
            for item, central in zip(items, layout.entries, strict=True):
                _require_zipinfo_matches_central(item, central)
                canonical, path = _require_canonical_source_zip_path(
                    item.filename,
                    is_directory=item.is_dir(),
                )
                if item.is_dir() and item.file_size != 0:
                    raise CodexBuildProvenanceError(
                        "source archive contains inconsistent directory metadata"
                    )
                if _is_excluded(path, is_directory=item.is_dir()):
                    raise CodexBuildProvenanceError(
                        "source archive contains a forbidden private or cache path"
                    )
                folded = canonical.casefold()
                if folded in names:
                    raise CodexBuildProvenanceError(
                        "source archive contains a canonical path collision"
                    )
                names.add(folded)
                unix_type = (item.external_attr >> 16) & 0o170000
                if unix_type == 0o120000:
                    raise CodexBuildProvenanceError(
                        "source archive contains a symbolic link"
                    )
                local_record = _require_safe_source_zip_local_record(
                    memoryview(content),
                    central,
                    central_directory_offset=layout.central_directory_offset,
                )
                occupied_ranges.append(
                    (local_record.record_start, local_record.record_end)
                )
                validated.append((item, canonical, central, local_record))
                if canonical == "factory-candidate.json":
                    if item.is_dir() or candidate_manifest_info is not None:
                        raise CodexBuildProvenanceError(
                            "source archive candidate manifest is ambiguous"
                        )
                    if item.file_size > MAX_FACTORY_CODEX_SOURCE_ZIP_MANIFEST_BYTES:
                        raise CodexBuildProvenanceError(
                            "source archive candidate manifest exceeds the size limit"
                        )
                    candidate_manifest_info = item
            _require_nonoverlapping_source_zip_records(occupied_ranges)
            if candidate_manifest_info is None:
                raise CodexBuildProvenanceError(
                    "source archive candidate manifest is missing"
                )
            candidate_manifest = _stream_source_zip_entries(
                memoryview(content),
                validated,
                candidate_manifest_info=candidate_manifest_info,
            )
            _require_json_object(candidate_manifest, label="candidate manifest")
            return candidate_manifest
    except CodexBuildProvenanceError:
        raise
    except (
        BadZipFile,
        LargeZipFile,
        RuntimeError,
        NotImplementedError,
        EOFError,
        OSError,
        UnicodeDecodeError,
        struct.error,
        zlib.error,
    ) as exc:
        raise CodexBuildProvenanceError("source archive is not a valid ZIP") from exc


def _preflight_source_zip(content: bytes) -> _SourceZipLayout:
    """Parse bounded raw ZIP metadata before stdlib creates any ZipInfo objects."""

    if len(content) > MAX_FACTORY_CODEX_SOURCE_ZIP_BYTES:
        raise CodexBuildProvenanceError("source archive exceeds the raw size limit")
    if len(content) < _ZIP_EOCD.size:
        raise CodexBuildProvenanceError("source archive is not a valid ZIP")
    view = memoryview(content)
    eocd_offset = _find_source_zip_eocd(view)
    (
        signature,
        disk_number,
        central_disk_number,
        entries_on_disk,
        entry_count,
        central_size,
        central_offset,
        _comment_length,
    ) = _ZIP_EOCD.unpack_from(view, eocd_offset)
    if signature != _ZIP_EOCD_SIGNATURE:
        raise CodexBuildProvenanceError("source archive EOCD is inconsistent")
    if (
        entries_on_disk == 0xFFFF
        or entry_count == 0xFFFF
        or central_size == 0xFFFFFFFF
        or central_offset == 0xFFFFFFFF
    ):
        raise CodexBuildProvenanceError("source archive uses unsupported ZIP64 metadata")
    if disk_number != 0 or central_disk_number != 0 or entries_on_disk != entry_count:
        raise CodexBuildProvenanceError("source archive must be a single-disk ZIP")
    if entry_count > MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES:
        raise CodexBuildProvenanceError("source archive exceeds the entry count limit")
    central_end = central_offset + central_size
    if central_offset > eocd_offset or central_end != eocd_offset:
        raise CodexBuildProvenanceError(
            "source archive central directory bounds are inconsistent"
        )

    entries: list[_SourceZipCentralEntry] = []
    cursor = central_offset
    aggregate_size = 0
    while cursor < central_end:
        if central_end - cursor < _ZIP_CENTRAL_HEADER.size:
            raise CodexBuildProvenanceError(
                "source archive central directory is truncated"
            )
        fields = _ZIP_CENTRAL_HEADER.unpack_from(view, cursor)
        (
            central_signature,
            _version_made_by,
            _version_needed,
            flags,
            compression,
            _modified_time,
            _modified_date,
            crc32,
            compressed_size,
            uncompressed_size,
            filename_length,
            extra_length,
            comment_length,
            entry_disk_number,
            _internal_attr,
            external_attr,
            local_header_offset,
        ) = fields
        if central_signature != _ZIP_CENTRAL_SIGNATURE:
            raise CodexBuildProvenanceError(
                "source archive central directory signature is invalid"
            )
        record_end = (
            cursor
            + _ZIP_CENTRAL_HEADER.size
            + filename_length
            + extra_length
            + comment_length
        )
        if record_end > central_end:
            raise CodexBuildProvenanceError(
                "source archive central directory record is out of bounds"
            )
        if (
            compressed_size == 0xFFFFFFFF
            or uncompressed_size == 0xFFFFFFFF
            or local_header_offset == 0xFFFFFFFF
            or entry_disk_number == 0xFFFF
        ):
            raise CodexBuildProvenanceError(
                "source archive uses unsupported ZIP64 metadata"
            )
        if entry_disk_number != 0:
            raise CodexBuildProvenanceError("source archive must be a single-disk ZIP")
        _require_safe_source_zip_flags(flags, compression)
        _require_safe_source_zip_sizes(
            compression=compression,
            compressed_size=compressed_size,
            uncompressed_size=uncompressed_size,
            content_size=len(content),
        )
        aggregate_size += uncompressed_size
        if aggregate_size > MAX_FACTORY_CODEX_SOURCE_ZIP_TOTAL_BYTES:
            raise CodexBuildProvenanceError(
                "source archive exceeds the aggregate uncompressed size limit"
            )
        filename_start = cursor + _ZIP_CENTRAL_HEADER.size
        extra_start = filename_start + filename_length
        filename = bytes(view[filename_start:extra_start])
        filename_encoding = "utf-8" if flags & _ZIP_UTF8_FLAG else "cp437"
        decoded_filename = filename.decode(filename_encoding)
        _require_canonical_source_zip_path(
            decoded_filename,
            is_directory=decoded_filename.endswith("/"),
        )
        _require_no_zip64_extra(
            view[extra_start : extra_start + extra_length],
        )
        if local_header_offset >= central_offset:
            raise CodexBuildProvenanceError(
                "source archive local header offset is out of bounds"
            )
        if len(entries) >= MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES:
            raise CodexBuildProvenanceError("source archive exceeds the entry count limit")
        entries.append(
            _SourceZipCentralEntry(
                filename=filename,
                flags=flags,
                compression=compression,
                crc32=crc32,
                compressed_size=compressed_size,
                uncompressed_size=uncompressed_size,
                disk_number=entry_disk_number,
                external_attr=external_attr,
                local_header_offset=local_header_offset,
            )
        )
        cursor = record_end
    if cursor != central_end or len(entries) != entry_count:
        raise CodexBuildProvenanceError(
            "source archive central directory count is inconsistent"
        )
    return _SourceZipLayout(
        central_directory_offset=central_offset,
        entries=tuple(entries),
    )


def _find_source_zip_eocd(view: memoryview) -> int:
    earliest = max(0, len(view) - _ZIP_EOCD.size - 0xFFFF)
    matches: list[int] = []
    for offset in range(len(view) - _ZIP_EOCD.size, earliest - 1, -1):
        if struct.unpack_from("<I", view, offset)[0] != _ZIP_EOCD_SIGNATURE:
            continue
        comment_length = struct.unpack_from("<H", view, offset + 20)[0]
        if offset + _ZIP_EOCD.size + comment_length == len(view):
            matches.append(offset)
            if len(matches) > 1:
                break
    if len(matches) != 1:
        raise CodexBuildProvenanceError("source archive EOCD is missing or ambiguous")
    return matches[0]


def _require_no_zip64_extra(extra: memoryview) -> None:
    cursor = 0
    while cursor < len(extra):
        if len(extra) - cursor < _ZIP_EXTRA_HEADER.size:
            raise CodexBuildProvenanceError("source archive extra field is truncated")
        extra_id, extra_size = _ZIP_EXTRA_HEADER.unpack_from(extra, cursor)
        cursor += _ZIP_EXTRA_HEADER.size
        end = cursor + extra_size
        if end > len(extra):
            raise CodexBuildProvenanceError("source archive extra field is out of bounds")
        if extra_id == _ZIP64_EXTRA_ID:
            raise CodexBuildProvenanceError(
                "source archive uses unsupported ZIP64 metadata"
            )
        cursor = end


def _require_safe_source_zip_flags(flags: int, compression: int) -> None:
    if compression not in _SUPPORTED_SOURCE_ZIP_COMPRESSION:
        raise CodexBuildProvenanceError(
            "source archive contains an unsupported compression method"
        )
    allowed = _ZIP_DATA_DESCRIPTOR_FLAG | _ZIP_UTF8_FLAG
    if compression == ZIP_DEFLATED:
        allowed |= _ZIP_DEFLATE_OPTION_FLAGS
    if flags & 0x0001:
        raise CodexBuildProvenanceError(
            "source archive must not contain encrypted entries"
        )
    if flags & ~allowed:
        raise CodexBuildProvenanceError("source archive contains unsupported ZIP flags")


def _require_safe_source_zip_sizes(
    *,
    compression: int,
    compressed_size: int,
    uncompressed_size: int,
    content_size: int,
) -> None:
    if compression == ZIP_STORED and compressed_size != uncompressed_size:
        raise CodexBuildProvenanceError(
            "source archive contains inconsistent entry metadata"
        )
    if (
        compressed_size < 0
        or uncompressed_size < 0
        or compressed_size > content_size
        or (uncompressed_size > 0 and compressed_size == 0)
    ):
        raise CodexBuildProvenanceError(
            "source archive contains inconsistent entry metadata"
        )
    if uncompressed_size > MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES:
        raise CodexBuildProvenanceError(
            "source archive entry exceeds the uncompressed size limit"
        )


def _require_zipinfo_matches_central(
    item: ZipInfo,
    central: _SourceZipCentralEntry,
) -> None:
    encoding = "utf-8" if central.flags & _ZIP_UTF8_FLAG else "cp437"
    decoded_filename = central.filename.decode(encoding)
    if (
        item.orig_filename != decoded_filename
        or item.filename != decoded_filename
        or item.flag_bits != central.flags
        or item.compress_type != central.compression
        or item.CRC != central.crc32
        or item.compress_size != central.compressed_size
        or item.file_size != central.uncompressed_size
        or item.header_offset != central.local_header_offset
        or item.external_attr != central.external_attr
    ):
        raise CodexBuildProvenanceError(
            "source archive contains inconsistent entry metadata"
        )


def _require_canonical_source_zip_path(
    filename: str,
    *,
    is_directory: bool,
) -> tuple[str, PurePosixPath]:
    if "\\" in filename:
        raise CodexBuildProvenanceError(
            "source archive contains a non-canonical path"
        )
    normalized = filename.replace("\\", "/")
    parts = normalized.split("/")
    if is_directory and parts and parts[-1] == "":
        parts = parts[:-1]
    if (
        ".." in parts
        or normalized.startswith("/")
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
    ):
        raise CodexBuildProvenanceError(
            "source archive contains path traversal"
        )
    if (
        not normalized
        or not parts
        or any(part in {"", "."} or "\x00" in part for part in parts)
    ):
        raise CodexBuildProvenanceError(
            "source archive contains a non-canonical path"
        )
    canonical = "/".join(parts)
    path = PurePosixPath(canonical)
    if path.is_absolute() or path.as_posix() != canonical:
        raise CodexBuildProvenanceError(
            "source archive contains a non-canonical path"
        )
    return canonical, path


def _require_safe_source_zip_local_record(
    view: memoryview,
    central: _SourceZipCentralEntry,
    *,
    central_directory_offset: int,
) -> _SourceZipLocalRecord:
    offset = central.local_header_offset
    if offset < 0 or offset + _ZIP_LOCAL_HEADER.size > central_directory_offset:
        raise CodexBuildProvenanceError(
            "source archive local header offset is out of bounds"
        )
    (
        signature,
        _version_needed,
        flags,
        compression,
        _modified_time,
        _modified_date,
        local_crc32,
        local_compressed_size,
        local_uncompressed_size,
        filename_length,
        extra_length,
    ) = _ZIP_LOCAL_HEADER.unpack_from(view, offset)
    if signature != _ZIP_LOCAL_SIGNATURE:
        raise CodexBuildProvenanceError("source archive local header is invalid")
    header_end = offset + _ZIP_LOCAL_HEADER.size + filename_length + extra_length
    if header_end > central_directory_offset:
        raise CodexBuildProvenanceError("source archive local header is out of bounds")
    filename_start = offset + _ZIP_LOCAL_HEADER.size
    extra_start = filename_start + filename_length
    if bytes(view[filename_start:extra_start]) != central.filename:
        raise CodexBuildProvenanceError(
            "source archive local header filename is inconsistent"
        )
    _require_no_zip64_extra(view[extra_start:header_end])
    if flags != central.flags or compression != central.compression:
        raise CodexBuildProvenanceError(
            "source archive contains inconsistent local header metadata"
        )
    uses_descriptor = bool(flags & _ZIP_DATA_DESCRIPTOR_FLAG)
    if uses_descriptor:
        if (
            local_crc32 not in {0, central.crc32}
            or local_compressed_size not in {0, central.compressed_size}
            or local_uncompressed_size not in {0, central.uncompressed_size}
        ):
                raise CodexBuildProvenanceError(
                    "source archive contains inconsistent local header metadata"
            )
    elif (
        local_crc32 != central.crc32
        or local_compressed_size != central.compressed_size
        or local_uncompressed_size != central.uncompressed_size
    ):
        raise CodexBuildProvenanceError(
            "source archive contains inconsistent local header metadata"
        )
    data_end = header_end + central.compressed_size
    if data_end > central_directory_offset:
        raise CodexBuildProvenanceError("source archive compressed data is out of bounds")
    record_end = data_end
    if uses_descriptor:
        record_end = _require_source_zip_data_descriptor(
            view,
            data_end,
            central,
            central_directory_offset=central_directory_offset,
        )
    return _SourceZipLocalRecord(
        record_start=offset,
        record_end=record_end,
        data_start=header_end,
        data_end=data_end,
    )


def _require_source_zip_data_descriptor(
    view: memoryview,
    offset: int,
    central: _SourceZipCentralEntry,
    *,
    central_directory_offset: int,
) -> int:
    if offset + _ZIP_DATA_DESCRIPTOR.size > central_directory_offset:
        raise CodexBuildProvenanceError("source archive data descriptor is truncated")
    expected = (
        central.crc32,
        central.compressed_size,
        central.uncompressed_size,
    )
    unsigned = _ZIP_DATA_DESCRIPTOR.unpack_from(view, offset)
    unsigned_matches = unsigned == expected
    signed_matches = False
    if (
        unsigned[0] == _ZIP_DATA_DESCRIPTOR_SIGNATURE
        and offset + 4 + _ZIP_DATA_DESCRIPTOR.size <= central_directory_offset
    ):
        signed_matches = (
            _ZIP_DATA_DESCRIPTOR.unpack_from(view, offset + 4) == expected
        )
    if unsigned_matches and signed_matches:
        raise CodexBuildProvenanceError(
            "source archive data descriptor layout is ambiguous"
        )
    if signed_matches:
        return offset + 4 + _ZIP_DATA_DESCRIPTOR.size
    if unsigned_matches:
        return offset + _ZIP_DATA_DESCRIPTOR.size
    raise CodexBuildProvenanceError(
        "source archive data descriptor is inconsistent"
    )


def _require_nonoverlapping_source_zip_records(
    ranges: Iterable[tuple[int, int]],
) -> None:
    previous_end = 0
    for start, end in sorted(ranges):
        if start < previous_end or end < start:
            raise CodexBuildProvenanceError("source archive local records overlap")
        previous_end = end


def _stream_source_zip_entries(
    view: memoryview,
    entries: Iterable[
        tuple[ZipInfo, str, _SourceZipCentralEntry, _SourceZipLocalRecord]
    ],
    *,
    candidate_manifest_info: ZipInfo,
) -> bytes:
    """Verify exact raw member streams while retaining only the bounded manifest."""

    manifest_chunks: list[bytes] = []
    aggregate_size = 0
    for item, _canonical, central, local_record in entries:
        observed_size = 0
        observed_crc32 = 0
        for chunk in _iter_source_zip_member_output(view, central, local_record):
            observed_size += len(chunk)
            aggregate_size += len(chunk)
            if (
                observed_size > central.uncompressed_size
                or observed_size > MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES
                or aggregate_size > MAX_FACTORY_CODEX_SOURCE_ZIP_TOTAL_BYTES
            ):
                raise CodexBuildProvenanceError(
                    "source archive expanded beyond its declared size"
                )
            observed_crc32 = zlib.crc32(chunk, observed_crc32)
            if item is candidate_manifest_info:
                if observed_size > MAX_FACTORY_CODEX_SOURCE_ZIP_MANIFEST_BYTES:
                    raise CodexBuildProvenanceError(
                        "source archive candidate manifest exceeds the size limit"
                    )
                manifest_chunks.append(chunk)
        if (
            observed_size != central.uncompressed_size
            or observed_crc32 & 0xFFFFFFFF != central.crc32
        ):
            raise CodexBuildProvenanceError(
                "source archive contains inconsistent entry metadata"
            )
    return b"".join(manifest_chunks)


def _iter_source_zip_member_output(
    view: memoryview,
    central: _SourceZipCentralEntry,
    local_record: _SourceZipLocalRecord,
) -> Iterable[bytes]:
    if central.compression == ZIP_STORED:
        cursor = local_record.data_start
        while cursor < local_record.data_end:
            end = min(cursor + _SOURCE_ZIP_READ_CHUNK_BYTES, local_record.data_end)
            yield bytes(view[cursor:end])
            cursor = end
        return

    decompressor = zlib.decompressobj(-zlib.MAX_WBITS)
    cursor = local_record.data_start
    while cursor < local_record.data_end:
        end = min(cursor + _SOURCE_ZIP_READ_CHUNK_BYTES, local_record.data_end)
        pending: bytes | memoryview = view[cursor:end]
        cursor = end
        while pending:
            pending_size = len(pending)
            output = decompressor.decompress(
                pending,
                _SOURCE_ZIP_READ_CHUNK_BYTES,
            )
            pending = decompressor.unconsumed_tail
            if decompressor.unused_data:
                raise CodexBuildProvenanceError(
                    "source archive DEFLATE member contains trailing stream data"
                )
            if output:
                yield output
            if pending and not output and len(pending) >= pending_size:
                raise CodexBuildProvenanceError(
                    "source archive DEFLATE member made no bounded progress"
                )
    while True:
        output = decompressor.decompress(b"", _SOURCE_ZIP_READ_CHUNK_BYTES)
        if not output:
            break
        yield output
    if (
        not decompressor.eof
        or decompressor.unused_data
        or decompressor.unconsumed_tail
    ):
        raise CodexBuildProvenanceError(
            "source archive DEFLATE member is truncated or inconsistent"
        )


def _require_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexBuildProvenanceError(f"{label} must be a UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise CodexBuildProvenanceError(f"{label} must be a UTF-8 JSON object")
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


__all__ = [
    "CaptainCodexBuildReceiptIssuer",
    "CodexBuildArtifactCas",
    "CodexBuildProvenanceError",
    "MAX_FACTORY_CODEX_SOURCE_ZIP_BYTES",
    "MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES",
    "MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES",
    "MAX_FACTORY_CODEX_SOURCE_ZIP_MANIFEST_BYTES",
    "MAX_FACTORY_CODEX_SOURCE_ZIP_TOTAL_BYTES",
]
