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
from collections.abc import Iterable
from datetime import datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zipfile import BadZipFile, ZIP_STORED, ZipFile, ZipInfo

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.forge_contracts import (
    ArtifactRef,
    CodexBuildReceiptV1,
)
from agenten.agent_factory.skill_workflow_contracts import CodexBuildBriefV1


_NAMESPACE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")
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
        _require_safe_source_zip(source_bytes)
        test_bytes = tuple(
            snapshot_files[path.relative_to(root).as_posix()] for path in evidence_files
        )
        for content in test_bytes:
            _require_json_object(content, label="test evidence")

        expected_source_ref = self._cas.reference_for(
            source_bytes,
            media_type="application/zip",
            namespace="codex-source",
        )
        _require_manifest_source_binding(manifest, expected_source_ref)

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


def _require_safe_source_zip(content: bytes) -> None:
    try:
        with ZipFile(BytesIO(content)) as archive:
            names: set[str] = set()
            for item in archive.infolist():
                normalized = item.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                if (
                    not normalized
                    or path.is_absolute()
                    or _WINDOWS_DRIVE_PATTERN.match(normalized)
                    or ".." in path.parts
                ):
                    raise CodexBuildProvenanceError(
                        "source archive contains path traversal"
                    )
                if _is_excluded(path, is_directory=item.is_dir()):
                    raise CodexBuildProvenanceError(
                        "source archive contains a forbidden private or cache path"
                    )
                folded = normalized.casefold()
                if folded in names:
                    raise CodexBuildProvenanceError(
                        "source archive contains duplicate paths"
                    )
                names.add(folded)
                unix_type = (item.external_attr >> 16) & 0o170000
                if unix_type == 0o120000:
                    raise CodexBuildProvenanceError(
                        "source archive contains a symbolic link"
                    )
                if item.flag_bits & 0x1:
                    raise CodexBuildProvenanceError(
                        "source archive must not contain encrypted entries"
                    )
    except BadZipFile as exc:
        raise CodexBuildProvenanceError("source archive is not a valid ZIP") from exc


def _require_json_object(content: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexBuildProvenanceError(f"{label} must be a UTF-8 JSON object") from exc
    if not isinstance(value, dict):
        raise CodexBuildProvenanceError(f"{label} must be a UTF-8 JSON object")
    return value


def _require_manifest_source_binding(
    manifest: dict[str, Any],
    source_ref: ArtifactRef,
) -> None:
    raw = manifest.get("source_archive_ref")
    try:
        bound = ArtifactRef.model_validate(raw)
    except ValueError as exc:
        raise CodexBuildProvenanceError(
            "candidate manifest must contain a valid source archive reference"
        ) from exc
    if bound != source_ref:
        raise CodexBuildProvenanceError(
            "candidate manifest does not match the sealed source archive"
        )


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
]
