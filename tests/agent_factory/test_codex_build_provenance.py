from __future__ import annotations

import hashlib
import json
import os
import struct
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo

import pytest

import agenten.agent_factory.codex_build_provenance as codex_build_provenance
from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
    CodexBuildProvenanceError,
    MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES,
    MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES,
    MAX_FACTORY_CODEX_SOURCE_ZIP_MANIFEST_BYTES,
    MAX_FACTORY_CODEX_SOURCE_ZIP_TOTAL_BYTES,
    _require_safe_source_zip,
)
from agenten.agent_factory.forge_contracts import codex_build_receipt_sha256
from agenten.agent_factory.skill_workflow_contracts import CodexBuildBriefV1
from tests.agent_factory.test_skill_workflow_contracts import brief_payload
from tests.agent_factory.test_state_machine import job_v3


NOW = datetime(2026, 7, 28, 18, 0, tzinfo=timezone.utc)


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        for name, content in sorted(files.items()):
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    return output.getvalue()


def _deflated_zip_bytes(files: list[tuple[str, bytes | int]]) -> bytes:
    """Build compressible fixtures without materializing large entry bodies."""

    output = BytesIO()
    chunk = b"x" * (64 * 1024)
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in files:
            info = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.compress_type = ZIP_DEFLATED
            with archive.open(info, "w") as target:
                if isinstance(content, bytes):
                    target.write(content)
                    continue
                remaining = content
                while remaining:
                    piece = chunk[: min(remaining, len(chunk))]
                    target.write(piece)
                    remaining -= len(piece)
    return output.getvalue()


def _eocd_offset(archive: bytes | bytearray) -> int:
    offset = archive.rfind(b"PK\x05\x06")
    assert offset >= 0
    return offset


def _central_header_offset(archive: bytes | bytearray, filename: bytes) -> int:
    filename_offset = archive.rfind(filename)
    offset = archive.rfind(b"PK\x01\x02", 0, filename_offset)
    assert offset >= 0
    return offset


def _local_header_offset(archive: bytes | bytearray, filename: str) -> int:
    with ZipFile(BytesIO(bytes(archive))) as source:
        return source.getinfo(filename).header_offset


class _NonSeekableZipOutput(BytesIO):
    def seekable(self) -> bool:
        return False

    def seek(self, *args: object, **kwargs: object) -> int:
        raise OSError("fixture output is intentionally non-seekable")


def _data_descriptor_zip_bytes(files: dict[str, bytes]) -> bytes:
    output = _NonSeekableZipOutput()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _bound_job_and_brief():
    payload = brief_payload(
        authorized_path_roots=["workspace://factory/workflow"]
    )
    brief = CodexBuildBriefV1.model_validate(payload)
    assignment = brief.build_assignment
    job = job_v3(mode="demo").model_copy(
        update={
            "job_id": brief.job_id,
            "correlation_id": brief.correlation_id,
            "subject_version": brief.subject_version,
            "compiled_spec_ref": assignment.compiled_spec_ref,
            "dependency_graph_ref": assignment.dependency_graph_ref,
            "acceptance_assertion_ids": assignment.public_assertion_ids,
            "deadline_at": assignment.deadline_at,
        }
    )
    return job, brief


def _workspace(tmp_path: Path, cas: CodexBuildArtifactCas) -> Path:
    workspace = tmp_path / "workspace"
    (workspace / "src").mkdir(parents=True)
    (workspace / "src" / "team.py").write_text("TEAM = 'claims'\n", encoding="utf-8")

    candidate_manifest = json.dumps(
        {
            "schema": "captain.factory-candidate.v1",
            "candidate_id": "claims_team_v1",
        },
        sort_keys=True,
    ).encode("utf-8")
    source_bytes = _zip_bytes(
        {
            "factory-candidate.json": candidate_manifest,
            "src/team.py": b"TEAM = 'claims'\n",
        }
    )
    (workspace / "candidate.zip").write_bytes(source_bytes)
    (workspace / "factory-candidate.json").write_bytes(candidate_manifest)
    (workspace / "test-evidence.json").write_text(
        json.dumps(
            {
                "command_id": "pytest.not-live",
                "status": "passed",
                "assertion_ids": ["schema_valid", "real_case_green"],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return workspace


def test_issuer_seals_deterministic_safe_workspace_and_exact_build_bindings(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    (workspace / ".env").write_text("DO_NOT_ARCHIVE=1", encoding="utf-8")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("private", encoding="utf-8")
    (workspace / "logs").mkdir()
    (workspace / "logs" / "provider.log").write_text("private", encoding="utf-8")
    (workspace / "__pycache__").mkdir()
    (workspace / "__pycache__" / "team.pyc").write_bytes(b"cache")
    job, brief = _bound_job_and_brief()
    issuer = CaptainCodexBuildReceiptIssuer(cas)

    first = issuer.issue(
        job=job,
        build_brief=brief,
        workspace_root=workspace,
        codex_session_receipt=b'{"provider":"codex","session_id":"session-123"}',
        seal_idempotency_key="7" * 64,
        candidate_manifest_path="factory-candidate.json",
        source_archive_path="candidate.zip",
        test_evidence_paths=("test-evidence.json",),
        completed_at=NOW,
    )
    os.utime(workspace / "src" / "team.py", (2_000_000_000, 2_000_000_000))
    replay = issuer.issue(
        job=job,
        build_brief=brief,
        workspace_root=workspace,
        codex_session_receipt=b'{"provider":"codex","session_id":"session-123"}',
        seal_idempotency_key="7" * 64,
        candidate_manifest_path="factory-candidate.json",
        source_archive_path="candidate.zip",
        test_evidence_paths=("test-evidence.json",),
        completed_at=NOW,
    )
    receipt_ref = issuer.persist_receipt(first)

    assert replay == first
    assert receipt_ref.media_type == "application/json"
    assert receipt_ref.sha256 == codex_build_receipt_sha256(first)
    assert cas.read_bytes(receipt_ref) == json.dumps(
        first.model_dump(mode="json", by_alias=True),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    assert first.factory_job_id == job.job_id
    assert first.creation_job_id == brief.build_assignment.creation_job_id
    assert first.assignment_id == brief.build_assignment.assignment_id
    assert first.idempotency_key == brief.build_assignment.idempotency_key
    assert first.seal_idempotency_key == "7" * 64
    assert first.workspace_ref == brief.build_assignment.workspace_ref
    assert first.build_brief_ref.model_dump(mode="json") == brief.artifact_ref.model_dump(
        mode="json"
    )
    assert first.acceptance_assertion_ids == job.acceptance_assertion_ids
    assert cas.read_bytes(first.candidate_manifest_ref) == (
        workspace / "factory-candidate.json"
    ).read_bytes()
    assert cas.read_bytes(first.source_archive_ref) == (
        workspace / "candidate.zip"
    ).read_bytes()
    assert cas.read_bytes(first.test_evidence_refs[0]) == (
        workspace / "test-evidence.json"
    ).read_bytes()

    with ZipFile(BytesIO(cas.read_bytes(first.workspace_snapshot_ref))) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert names == [
            "candidate.zip",
            "factory-candidate.json",
            "src/team.py",
            "test-evidence.json",
        ]
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())


def test_issuer_fails_closed_for_changed_binding_or_unbound_candidate_archive(
    tmp_path: Path,
) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    job, brief = _bound_job_and_brief()
    issuer = CaptainCodexBuildReceiptIssuer(cas)

    changed_job = job.model_copy(update={"correlation_id": job.event_id})
    with pytest.raises(CodexBuildProvenanceError, match="job.*brief"):
        issuer.issue(
            job=changed_job,
            build_brief=brief,
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"session-123"}',
            seal_idempotency_key="7" * 64,
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )

    manifest = json.loads((workspace / "factory-candidate.json").read_text("utf-8"))
    manifest["candidate_id"] = "substituted_team_v1"
    (workspace / "factory-candidate.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(CodexBuildProvenanceError, match="candidate manifest.*source archive"):
        issuer.issue(
            job=job,
            build_brief=brief,
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"session-123"}',
            seal_idempotency_key="7" * 64,
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )


def test_snapshot_rejects_symlinks_traversal_and_unsafe_source_zip(tmp_path: Path) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    workspace = _workspace(tmp_path, cas)
    job, brief = _bound_job_and_brief()
    issuer = CaptainCodexBuildReceiptIssuer(cas)

    with pytest.raises(CodexBuildProvenanceError, match="relative.*workspace"):
        issuer.issue(
            job=job,
            build_brief=brief,
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"session-123"}',
            seal_idempotency_key="7" * 64,
            candidate_manifest_path="../outside.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )

    (workspace / ".env").write_text('{"source_archive_ref": {}}', encoding="utf-8")
    with pytest.raises(CodexBuildProvenanceError, match="forbidden"):
        issuer.issue(
            job=job,
            build_brief=brief,
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"session-123"}',
            seal_idempotency_key="7" * 64,
            candidate_manifest_path=".env",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )

    unsafe_source = _zip_bytes({"../escape.py": b"bad"})
    (workspace / "candidate.zip").write_bytes(unsafe_source)
    with pytest.raises(CodexBuildProvenanceError, match="source archive.*traversal"):
        issuer.issue(
            job=job,
            build_brief=brief,
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"session-123"}',
            seal_idempotency_key="7" * 64,
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )

    (workspace / "candidate.zip").write_bytes(_zip_bytes({"src/team.py": b"safe"}))
    target = tmp_path / "outside.py"
    target.write_text("outside", encoding="utf-8")
    link = workspace / "src" / "linked.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("filesystem does not permit symlinks")
    with pytest.raises(CodexBuildProvenanceError, match="symbolic link"):
        issuer.issue(
            job=job,
            build_brief=brief,
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"session-123"}',
            seal_idempotency_key="7" * 64,
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )


def test_source_zip_accepts_each_exact_uncompressed_size_bound() -> None:
    manifest_prefix = b'{"padding":"'
    manifest_suffix = b'"}'
    manifest = (
        manifest_prefix
        + b"x"
        * (
            MAX_FACTORY_CODEX_SOURCE_ZIP_MANIFEST_BYTES
            - len(manifest_prefix)
            - len(manifest_suffix)
        )
        + manifest_suffix
    )
    archive = _deflated_zip_bytes(
        [
            ("factory-candidate.json", manifest),
            ("src/exact-entry.bin", MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES),
        ]
    )

    assert _require_safe_source_zip(archive) == manifest


def test_source_zip_accepts_exact_entry_count_bound() -> None:
    files: list[tuple[str, bytes | int]] = [("factory-candidate.json", b"{}")]
    files.extend(
        (f"src/empty-{index:04d}.txt", b"")
        for index in range(MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES - 1)
    )

    assert _require_safe_source_zip(_deflated_zip_bytes(files)) == b"{}"


def test_source_zip_accepts_exact_aggregate_uncompressed_size_bound() -> None:
    manifest = b"{}"
    fourth_entry_size = (
        MAX_FACTORY_CODEX_SOURCE_ZIP_TOTAL_BYTES
        - len(manifest)
        - 3 * MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES
    )
    archive = _deflated_zip_bytes(
        [
            ("factory-candidate.json", manifest),
            ("src/part-1.bin", MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES),
            ("src/part-2.bin", MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES),
            ("src/part-3.bin", MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES),
            ("src/part-4.bin", fourth_entry_size),
        ]
    )

    assert _require_safe_source_zip(archive) == manifest


def test_source_zip_rejects_manifest_over_json_limit_before_decode() -> None:
    oversized_manifest = (
        b'{"padding":"'
        + b"x" * MAX_FACTORY_CODEX_SOURCE_ZIP_MANIFEST_BYTES
        + b'"}'
    )
    archive = _deflated_zip_bytes(
        [("factory-candidate.json", oversized_manifest)]
    )
    assert len(archive) < len(oversized_manifest) // 100

    with pytest.raises(CodexBuildProvenanceError, match="candidate manifest.*size"):
        _require_safe_source_zip(archive)


def test_source_zip_rejects_entry_over_uncompressed_size_limit() -> None:
    archive = _deflated_zip_bytes(
        [
            ("factory-candidate.json", b"{}"),
            ("src/oversized.bin", MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES + 1),
        ]
    )

    with pytest.raises(CodexBuildProvenanceError, match="entry.*size"):
        _require_safe_source_zip(archive)


def test_source_zip_rejects_aggregate_uncompressed_size_limit() -> None:
    files: list[tuple[str, bytes | int]] = [("factory-candidate.json", b"{}")]
    files.extend(
        (f"src/part-{index}.bin", MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES)
        for index in range(
            MAX_FACTORY_CODEX_SOURCE_ZIP_TOTAL_BYTES
            // MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRY_BYTES
        )
    )

    with pytest.raises(CodexBuildProvenanceError, match="aggregate.*size"):
        _require_safe_source_zip(_deflated_zip_bytes(files))


def test_source_zip_rejects_entry_count_limit() -> None:
    files: list[tuple[str, bytes | int]] = [("factory-candidate.json", b"{}")]
    files.extend(
        (f"src/empty-{index:04d}.txt", b"")
        for index in range(MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES)
    )

    with pytest.raises(CodexBuildProvenanceError, match="entry count"):
        _require_safe_source_zip(_deflated_zip_bytes(files))


def test_source_zip_rejects_unsupported_compression_before_read() -> None:
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_BZIP2) as archive:
        archive.writestr("factory-candidate.json", b"{}")

    with pytest.raises(CodexBuildProvenanceError, match="unsupported compression"):
        _require_safe_source_zip(output.getvalue())


def test_source_zip_rejects_inconsistent_stored_entry_sizes_before_read() -> None:
    archive = bytearray(
        _zip_bytes(
            {
                "factory-candidate.json": b"{}",
                "src/team.py": b"x",
            }
        )
    )
    filename_offset = archive.rfind(b"src/team.py")
    central_header_offset = archive.rfind(b"PK\x01\x02", 0, filename_offset)
    assert central_header_offset >= 0
    struct.pack_into("<I", archive, central_header_offset + 24, 2)

    with pytest.raises(CodexBuildProvenanceError, match="inconsistent.*metadata"):
        _require_safe_source_zip(bytes(archive))


def test_source_zip_preflight_rejects_eocd_entry_count_over_limit() -> None:
    archive = bytearray(_zip_bytes({"factory-candidate.json": b"{}"}))
    eocd = _eocd_offset(archive)
    struct.pack_into(
        "<HH",
        archive,
        eocd + 8,
        MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES + 1,
        MAX_FACTORY_CODEX_SOURCE_ZIP_ENTRIES + 1,
    )

    with pytest.raises(CodexBuildProvenanceError, match="entry count"):
        _require_safe_source_zip(bytes(archive))


def test_source_zip_preflight_rejects_eocd_and_actual_count_mismatch() -> None:
    archive = bytearray(
        _zip_bytes(
            {
                "factory-candidate.json": b"{}",
                "src/team.py": b"safe",
            }
        )
    )
    eocd = _eocd_offset(archive)
    struct.pack_into("<HH", archive, eocd + 8, 1, 1)

    with pytest.raises(CodexBuildProvenanceError, match="central directory.*count"):
        _require_safe_source_zip(bytes(archive))


def test_source_zip_preflight_counts_4097_records_before_zipfile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    files = {"factory-candidate.json": b"{}"}
    files.update({f"src/empty-{index:04d}.txt": b"" for index in range(4096)})
    archive = bytearray(_zip_bytes(files))
    eocd = _eocd_offset(archive)
    struct.pack_into("<HH", archive, eocd + 8, 1, 1)

    def unexpected_zipfile(*args: object, **kwargs: object) -> ZipFile:
        raise AssertionError("ZipFile must not see an over-count central directory")

    monkeypatch.setattr(codex_build_provenance, "ZipFile", unexpected_zipfile)
    with pytest.raises(CodexBuildProvenanceError, match="entry count"):
        _require_safe_source_zip(bytes(archive))


def test_source_zip_preflight_rejects_zip64_sentinel_and_extra() -> None:
    sentinel_archive = bytearray(_zip_bytes({"factory-candidate.json": b"{}"}))
    eocd = _eocd_offset(sentinel_archive)
    struct.pack_into("<HH", sentinel_archive, eocd + 8, 0xFFFF, 0xFFFF)
    with pytest.raises(CodexBuildProvenanceError, match="ZIP64"):
        _require_safe_source_zip(bytes(sentinel_archive))

    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_STORED) as archive:
        manifest = ZipInfo("factory-candidate.json")
        manifest.compress_type = ZIP_STORED
        archive.writestr(manifest, b"{}")
        source = ZipInfo("src/team.py")
        source.compress_type = ZIP_STORED
        source.extra = struct.pack("<HHQ", 0x0001, 8, 1)
        archive.writestr(source, b"safe")
    with pytest.raises(CodexBuildProvenanceError, match="ZIP64"):
        _require_safe_source_zip(output.getvalue())


@pytest.mark.parametrize("mutation", ["central_size", "central_signature"])
def test_source_zip_preflight_rejects_invalid_central_directory_bounds(
    mutation: str,
) -> None:
    archive = bytearray(_zip_bytes({"factory-candidate.json": b"{}"}))
    eocd = _eocd_offset(archive)
    if mutation == "central_size":
        central_size = struct.unpack_from("<I", archive, eocd + 12)[0]
        struct.pack_into("<I", archive, eocd + 12, central_size + 1)
    else:
        central_offset = struct.unpack_from("<I", archive, eocd + 16)[0]
        struct.pack_into("<I", archive, central_offset, 0)

    with pytest.raises(CodexBuildProvenanceError, match="central directory"):
        _require_safe_source_zip(bytes(archive))


@pytest.mark.parametrize(
    "unsafe_names",
    [
        ("./factory-candidate.json", "factory-candidate.json"),
        ("src//team.py",),
        ("src/./team.py",),
        ("src", "src/"),
    ],
)
def test_source_zip_rejects_noncanonical_or_colliding_paths(
    unsafe_names: tuple[str, ...],
) -> None:
    files = {"factory-candidate.json": b"{}"}
    files.update({name: b"" if name.endswith("/") else b"{}" for name in unsafe_names})

    with pytest.raises(CodexBuildProvenanceError, match="canonical path"):
        _require_safe_source_zip(_zip_bytes(files))


def test_source_zip_rejects_raw_backslash_path() -> None:
    archive = _zip_bytes(
        {
            "factory-candidate.json": b"{}",
            "src/team.py": b"safe",
        }
    ).replace(b"src/team.py", b"src\\team.py")

    with pytest.raises(CodexBuildProvenanceError, match="canonical path"):
        _require_safe_source_zip(archive)


def test_source_zip_rejects_directory_entry_with_file_content() -> None:
    archive = _zip_bytes(
        {
            "factory-candidate.json": b"{}",
            "src/": b"not-a-directory-body",
        }
    )

    with pytest.raises(CodexBuildProvenanceError, match="inconsistent.*metadata"):
        _require_safe_source_zip(archive)


def test_source_zip_rejects_forged_local_header_offset() -> None:
    archive = bytearray(
        _zip_bytes(
            {
                "factory-candidate.json": b"{}",
                "src/team.py": b"safe",
            }
        )
    )
    source_central = _central_header_offset(archive, b"src/team.py")
    manifest_local = _local_header_offset(archive, "factory-candidate.json")
    struct.pack_into("<I", archive, source_central + 42, manifest_local)

    with pytest.raises(CodexBuildProvenanceError, match="local header|overlap"):
        _require_safe_source_zip(bytes(archive))


@pytest.mark.parametrize(
    ("unsafe_flag", "message"),
    [
        (0x0001, "encrypted"),
        (0x0020, "unsupported ZIP flags"),
        (0x0040, "unsupported ZIP flags"),
        (0x2000, "unsupported ZIP flags"),
    ],
)
def test_source_zip_rejects_unsafe_flags_before_streaming(
    unsafe_flag: int,
    message: str,
) -> None:
    archive = bytearray(
        _zip_bytes(
            {
                "factory-candidate.json": b"{}",
                "src/team.py": b"safe",
            }
        )
    )
    central = _central_header_offset(archive, b"src/team.py")
    local = _local_header_offset(archive, "src/team.py")
    central_flags = struct.unpack_from("<H", archive, central + 8)[0]
    local_flags = struct.unpack_from("<H", archive, local + 6)[0]
    struct.pack_into("<H", archive, central + 8, central_flags | unsafe_flag)
    struct.pack_into("<H", archive, local + 6, local_flags | unsafe_flag)

    with pytest.raises(CodexBuildProvenanceError, match=message):
        _require_safe_source_zip(bytes(archive))


def test_source_zip_streams_every_entry_and_rejects_bad_crc() -> None:
    archive = bytearray(
        _zip_bytes(
            {
                "factory-candidate.json": b"{}",
                "src/team.py": b"safe",
            }
        )
    )
    central = _central_header_offset(archive, b"src/team.py")
    local = _local_header_offset(archive, "src/team.py")
    crc = struct.unpack_from("<I", archive, central + 16)[0]
    forged_crc = crc ^ 0xFFFFFFFF
    struct.pack_into("<I", archive, central + 16, forged_crc)
    struct.pack_into("<I", archive, local + 14, forged_crc)

    with pytest.raises(CodexBuildProvenanceError, match="not a valid ZIP"):
        _require_safe_source_zip(bytes(archive))


def test_source_zip_accepts_utf8_names_and_data_descriptors() -> None:
    manifest = b'{"schema":"captain.factory-candidate.v1"}'
    archive = _data_descriptor_zip_bytes(
        {
            "factory-candidate.json": manifest,
            "src/über-team.py": b"TEAM = 'claims'\n",
        }
    )

    assert _require_safe_source_zip(archive) == manifest


def test_cas_is_content_addressed_write_once_and_detects_tampering(tmp_path: Path) -> None:
    cas = CodexBuildArtifactCas(tmp_path / "cas")
    reference = cas.put_bytes(
        b"sealed evidence", media_type="application/json", namespace="tests"
    )
    assert reference.sha256 == hashlib.sha256(b"sealed evidence").hexdigest()
    assert cas.put_bytes(
        b"sealed evidence", media_type="application/json", namespace="tests"
    ) == reference

    cas.local_path(reference).write_bytes(b"tampered")
    with pytest.raises(CodexBuildProvenanceError, match="CAS.*digest"):
        cas.read_bytes(reference)
    with pytest.raises(CodexBuildProvenanceError, match="write-once"):
        cas.put_bytes(
            b"sealed evidence", media_type="application/json", namespace="tests"
        )
