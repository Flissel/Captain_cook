from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import pytest

from agenten.agent_factory.codex_build_provenance import (
    CaptainCodexBuildReceiptIssuer,
    CodexBuildArtifactCas,
    CodexBuildProvenanceError,
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

    source_bytes = _zip_bytes({"src/team.py": b"TEAM = 'claims'\n"})
    source_ref = cas.put_bytes(
        source_bytes,
        media_type="application/zip",
        namespace="codex-source",
    )
    (workspace / "candidate.zip").write_bytes(source_bytes)
    (workspace / "factory-candidate.json").write_text(
        json.dumps(
            {
                "candidate_id": "claims_team_v1",
                "source_archive_ref": source_ref.model_dump(mode="json"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
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
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )

    manifest = json.loads((workspace / "factory-candidate.json").read_text("utf-8"))
    manifest["source_archive_ref"]["sha256"] = "f" * 64
    (workspace / "factory-candidate.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )
    with pytest.raises(CodexBuildProvenanceError, match="candidate manifest.*source archive"):
        issuer.issue(
            job=job,
            build_brief=brief,
            workspace_root=workspace,
            codex_session_receipt=b'{"session_id":"session-123"}',
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
            candidate_manifest_path="factory-candidate.json",
            source_archive_path="candidate.zip",
            test_evidence_paths=("test-evidence.json",),
            completed_at=NOW,
        )


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
