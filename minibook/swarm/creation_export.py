"""Fail-closed, local-only export for Agent Factory candidates."""
from __future__ import annotations

import io
import hashlib
import json
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


_CANDIDATE_MANIFEST = PurePosixPath("factory-candidate.json")
_SKILL_USAGE_RECEIPT = PurePosixPath(
    "evidence/hermes-factory-skill-usage-receipt.json"
)
_FIXED_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_EXCLUDED_DIRECTORY_NAMES = {
    ".git",
    ".pytest_cache",
    "__pycache__",
    "logs",
    "transcripts",
}
_EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo"}
_MAX_CAPTAIN_ARCHIVE_FILES = 4096
_MAX_CAPTAIN_ARCHIVE_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
_MAX_CANDIDATE_MANIFEST_BYTES = 1024 * 1024


class CreationExportError(RuntimeError):
    """The local candidate output cannot be exported safely."""


class CaptainSealedSourceError(ValueError):
    """A Captain-provided Codex archive failed its immutable import contract."""


def read_captain_sealed_source_archive(
    source_path: Path,
    *,
    expected_sha256: str,
    expected_candidate_manifest_sha256: str,
) -> tuple[bytes, dict[str, Any]]:
    """Verify and read a Captain-sealed ZIP without rewriting its bytes."""

    path = Path(source_path)
    if path.is_symlink() or not path.is_file():
        raise CaptainSealedSourceError("Captain source archive file is unavailable")
    try:
        source_archive = path.read_bytes()
    except OSError as exc:
        raise CaptainSealedSourceError("Captain source archive could not be read") from exc
    if hashlib.sha256(source_archive).hexdigest() != expected_sha256:
        raise CaptainSealedSourceError("Captain source archive digest does not match")

    try:
        with zipfile.ZipFile(io.BytesIO(source_archive)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > _MAX_CAPTAIN_ARCHIVE_FILES:
                raise CaptainSealedSourceError("Captain source archive structure is unsafe")
            normalized_names: set[str] = set()
            casefolded_names: set[str] = set()
            total_size = 0
            manifest_info: zipfile.ZipInfo | None = None
            for info in infos:
                name = info.filename
                if (
                    "\\" in name
                    or "\x00" in name
                    or (len(name) >= 2 and name[0].isalpha() and name[1] == ":")
                ):
                    raise CaptainSealedSourceError("Captain source archive path is unsafe")
                member = PurePosixPath(name)
                if member.is_absolute() or not member.parts or ".." in member.parts:
                    raise CaptainSealedSourceError("Captain source archive path is unsafe")
                normalized = member.as_posix().rstrip("/")
                if not normalized or normalized in normalized_names:
                    raise CaptainSealedSourceError("Captain source archive structure is unsafe")
                casefolded = normalized.casefold()
                if casefolded in casefolded_names:
                    raise CaptainSealedSourceError("Captain source archive path collision is unsafe")
                normalized_names.add(normalized)
                casefolded_names.add(casefolded)
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK or info.flag_bits & 0x1:
                    raise CaptainSealedSourceError("Captain source archive member is unsafe")
                total_size += info.file_size
                if total_size > _MAX_CAPTAIN_ARCHIVE_UNCOMPRESSED_BYTES:
                    raise CaptainSealedSourceError("Captain source archive is too large")
                if casefolded == _SKILL_USAGE_RECEIPT.as_posix().casefold():
                    raise CaptainSealedSourceError(
                        "Captain source archive contains the external skill usage receipt"
                    )
                if normalized == _CANDIDATE_MANIFEST.as_posix():
                    if info.is_dir() or info.file_size > _MAX_CANDIDATE_MANIFEST_BYTES:
                        raise CaptainSealedSourceError("Captain candidate manifest is unsafe")
                    manifest_info = info
            if manifest_info is None:
                raise CaptainSealedSourceError("Captain candidate manifest is missing")
            try:
                candidate_manifest_bytes = archive.read(manifest_info)
                candidate_manifest = json.loads(candidate_manifest_bytes)
            except (UnicodeDecodeError, json.JSONDecodeError, RuntimeError) as exc:
                raise CaptainSealedSourceError("Captain candidate manifest is invalid JSON") from exc
    except zipfile.BadZipFile as exc:
        raise CaptainSealedSourceError("Captain source archive is not a ZIP") from exc

    if not isinstance(candidate_manifest, dict):
        raise CaptainSealedSourceError("Captain candidate manifest must be a JSON object")
    if hashlib.sha256(candidate_manifest_bytes).hexdigest() != (
        expected_candidate_manifest_sha256
    ):
        raise CaptainSealedSourceError("Captain candidate manifest digest does not match")
    if candidate_manifest.get("schema", candidate_manifest.get("schema_name")) != (
        "captain.factory-candidate.v1"
    ):
        raise CaptainSealedSourceError("Captain candidate manifest schema is invalid")
    if "source_archive_ref" in candidate_manifest:
        raise CaptainSealedSourceError(
            "Captain candidate manifest must not contain publisher-owned source_archive_ref"
        )
    return source_archive, candidate_manifest


def build_creation_export(
    output_path: Path,
) -> tuple[bytes, dict[str, Any], bytes]:
    """Return deterministic archive bytes plus validated Factory evidence.

    The function performs local filesystem reads only. It intentionally has no
    Git, GitHub, Minibook, session, or network integration.
    """

    root = Path(output_path)
    if root.is_symlink():
        raise CreationExportError("creation output path must not be a symlink")
    if not root.is_dir():
        raise CreationExportError("creation output path is missing or not a directory")

    files = _collect_safe_files(root)
    candidate_path = root / Path(*_CANDIDATE_MANIFEST.parts)
    receipt_path = root / Path(*_SKILL_USAGE_RECEIPT.parts)
    candidate_manifest = _read_json_object(candidate_path, _CANDIDATE_MANIFEST)
    skill_usage_receipt = _read_json_object(receipt_path, _SKILL_USAGE_RECEIPT)

    if "source_archive_ref" in candidate_manifest:
        raise CreationExportError(
            "factory-candidate.json must not contain publisher-owned source_archive_ref"
        )

    canonical_candidate = _canonical_json(candidate_manifest)
    canonical_receipt = _canonical_json(skill_usage_receipt)
    archive_contents: dict[PurePosixPath, bytes] = {}
    for relative_path, file_path in files:
        if relative_path == _CANDIDATE_MANIFEST:
            archive_contents[relative_path] = canonical_candidate
        elif relative_path == _SKILL_USAGE_RECEIPT:
            archive_contents[relative_path] = canonical_receipt
        else:
            archive_contents[relative_path] = _read_bytes(file_path, relative_path)

    # Presence is checked again against the safe file set: a required file may
    # exist but still be disallowed (for example because it is a symlink).
    for required in (_CANDIDATE_MANIFEST, _SKILL_USAGE_RECEIPT):
        if required not in archive_contents:
            raise CreationExportError(f"required creation evidence is missing: {required}")

    return (
        _build_deterministic_zip(archive_contents),
        candidate_manifest,
        canonical_receipt,
    )


def _collect_safe_files(root: Path) -> list[tuple[PurePosixPath, Path]]:
    collected: list[tuple[PurePosixPath, Path]] = []
    try:
        paths = sorted(root.rglob("*"), key=lambda path: path.as_posix())
    except OSError as exc:
        raise CreationExportError("creation output could not be enumerated") from exc

    for path in paths:
        relative = _validated_relative_path(root, path)
        if path.is_symlink():
            raise CreationExportError(f"creation output contains a symlink: {relative}")
        if path.is_dir():
            continue
        if _is_excluded(relative):
            continue
        if not path.is_file():
            raise CreationExportError(
                f"creation output contains a non-regular file: {relative}"
            )
        collected.append((relative, path))
    return collected


def _validated_relative_path(root: Path, path: Path) -> PurePosixPath:
    try:
        relative = PurePosixPath(path.relative_to(root).as_posix())
    except ValueError as exc:
        raise CreationExportError("creation output contains a path outside its root") from exc
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise CreationExportError("creation output contains an unsafe path")
    return relative


def _is_excluded(path: PurePosixPath) -> bool:
    lowered_parts = tuple(part.casefold() for part in path.parts)
    if any(part in _EXCLUDED_DIRECTORY_NAMES for part in lowered_parts[:-1]):
        return True
    if any(part == ".env" or part.startswith(".env.") for part in lowered_parts):
        return True
    if any("transcript" in part for part in lowered_parts):
        return True
    filename = lowered_parts[-1]
    if filename in _EXCLUDED_DIRECTORY_NAMES:
        return True
    return PurePosixPath(filename).suffix in _EXCLUDED_SUFFIXES


def _read_json_object(path: Path, label: PurePosixPath) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CreationExportError(f"required creation evidence is missing: {label}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CreationExportError(f"required creation evidence is invalid JSON: {label}") from exc
    if not isinstance(value, dict):
        raise CreationExportError(f"required creation evidence must be a JSON object: {label}")
    return value


def _read_bytes(path: Path, label: PurePosixPath) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise CreationExportError(f"creation output file could not be read: {label}") from exc


def _canonical_json(value: dict[str, Any]) -> bytes:
    try:
        return (
            json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            + b"\n"
        )
    except (TypeError, ValueError) as exc:
        raise CreationExportError("required creation evidence is not canonical JSON") from exc


def _build_deterministic_zip(contents: dict[PurePosixPath, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        for path in sorted(contents, key=str):
            if path.is_absolute() or not path.parts or ".." in path.parts:
                raise CreationExportError("creation archive contains an unsafe path")
            info = zipfile.ZipInfo(str(path), date_time=_FIXED_ZIP_TIMESTAMP)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(
                info,
                contents[path],
                compress_type=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            )
    return buffer.getvalue()
