from __future__ import annotations

import hashlib
import io
import json
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from agenten.agent_factory.contracts import AgentFactoryJobV2
from agenten.agent_factory.forge_contracts import CreationResultV1
from agenten.agent_factory.outcome_contracts import CapabilityPackageManifestV1
from agenten.agent_factory.outcome_validation import (
    CapabilityPackageValidationError,
    CapabilityPackageValidator,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _fixture(path: str) -> dict[str, object]:
    return json.loads((FIXTURES / path).read_text(encoding="utf-8"))


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return output.getvalue()


def _ref(content: bytes, media_type: str) -> dict[str, str]:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "uri": f"artifact://validator-test/{digest}",
        "sha256": digest,
        "media_type": media_type,
    }


@dataclass
class RecordingContentStore:
    content_by_uri: dict[str, bytes]
    reads: list[str] = field(default_factory=list)

    async def read(self, reference: object) -> bytes:
        uri = str(getattr(reference, "uri"))
        self.reads.append(uri)
        return self.content_by_uri[uri]


@dataclass(frozen=True)
class ValidationCase:
    job: AgentFactoryJobV2
    creation_result: CreationResultV1
    package: CapabilityPackageManifestV1
    store: RecordingContentStore
    files: dict[str, bytes]


def _validation_case(
    *,
    autogen_source: bytes = b"CAPABILITY_ID = 'customer_support_triage'\n",
    package_test: bytes = (
        b"from autogen.team import CAPABILITY_ID\n\n"
        b"def test_generated_team_identity():\n"
        b"    assert CAPABILITY_ID == 'customer_support_triage'\n"
    ),
    team_manifest_updates: dict[str, object] | None = None,
) -> ValidationCase:
    job_payload = _fixture("agent_factory/agent_factory_job.v2.json")
    job = AgentFactoryJobV2.model_validate(job_payload)

    team_manifest: dict[str, object] = {
        "schema": "autogen-team.v1",
        "capability_id": job.required_capability,
        "capability_version": 1,
        "autogen_modules": ["autogen/__init__.py", "autogen/team.py"],
        "test_paths": ["tests/test_support_triage.py"],
    }
    team_manifest.update(team_manifest_updates or {})
    files = {
        "team-manifest.json": json.dumps(
            team_manifest, sort_keys=True, separators=(",", ":")
        ).encode("utf-8"),
        "autogen/__init__.py": b"# Sealed generated package.\n",
        "autogen/team.py": autogen_source,
        "skills/support_triage/SKILL.md": b"# Support triage skill\n",
        "tests/test_support_triage.py": package_test,
        "evidence/summary.json": b'{"status":"candidate"}\n',
        "RUNBOOK.md": b"# Runbook\n",
    }
    archive = _zip_bytes(files)
    source_ref = _ref(archive, "application/zip")

    kind_by_path = {
        "team-manifest.json": "team_manifest",
        "autogen/__init__.py": "autogen_source",
        "autogen/team.py": "autogen_source",
        "skills/support_triage/SKILL.md": "skill",
        "tests/test_support_triage.py": "test",
        "evidence/summary.json": "evidence",
        "RUNBOOK.md": "runbook",
    }
    media_type_by_path = {
        "team-manifest.json": "application/json",
        "autogen/__init__.py": "text/x-python",
        "autogen/team.py": "text/x-python",
        "skills/support_triage/SKILL.md": "text/markdown",
        "tests/test_support_triage.py": "text/x-python",
        "evidence/summary.json": "application/json",
        "RUNBOOK.md": "text/markdown",
    }
    artifacts = [
        {
            "path": path,
            "kind": kind_by_path[path],
            "reference": _ref(content, media_type_by_path[path]),
        }
        for path, content in files.items()
    ]
    artifact_ref_by_path = {
        str(item["path"]): item["reference"] for item in artifacts
    }

    assertion_evidence = _ref(b'{"assertion":"passed"}', "application/json")
    holdout_evidence = _ref(b'{"holdout":"passed"}', "application/json")
    recovery_evidence = _ref(b'{"recovery":"passed"}', "application/json")
    e2e_refs = [
        _ref(f'{{"e2e":{index}}}'.encode(), "application/json")
        for index in range(1, 4)
    ]
    skill_receipt = _ref(b'{"skill_usage":"accepted"}', "application/json")
    package_payload = {
        "schema": "captain.capability-package.v1",
        "capability_id": job.required_capability,
        "capability_version": 1,
        "factory_job_id": str(job.job_id),
        "creation_job_id": "11111111-1111-4111-8111-111111111111",
        "correlation_id": str(job.correlation_id),
        "subject_version": job.subject_version,
        "source_ref": source_ref,
        "team_manifest_ref": artifact_ref_by_path["team-manifest.json"],
        "artifacts": artifacts,
        "assertion_outcomes": [
            {
                "assertion_id": job.acceptance_assertion_ids[0],
                "status": "passed",
                "integration_intent": "none",
                "evidence_refs": [assertion_evidence],
            }
        ],
        "private_holdout_receipts": [
            {
                "holdout_id": job.private_holdout_refs[0].holdout_id,
                "assertion_id": job.acceptance_assertion_ids[0],
                "status": "passed",
                "evidence_ref": holdout_evidence,
            }
        ],
        "recovery_receipt": {
            "recovery_id": "controlled-recovery-01",
            "assertion_id": job.acceptance_assertion_ids[0],
            "status": "passed",
            "evidence_ref": recovery_evidence,
        },
        "release_evidence_refs": [recovery_evidence, *e2e_refs],
        "skill_usage_receipt_ref": skill_receipt,
        "tool_gaps": [],
        "runbook_ref": artifact_ref_by_path["RUNBOOK.md"],
    }
    package = CapabilityPackageManifestV1.model_validate(package_payload)
    package_bytes = package.model_dump_json(by_alias=True).encode("utf-8")
    package_manifest_ref = _ref(package_bytes, "application/json")

    creation_payload = _fixture("contracts/minibook_creation_result.v1.json")
    creation_payload.update(
        {
            "creation_job_id": str(package.creation_job_id),
            "correlation_id": str(job.correlation_id),
            "subject_version": job.subject_version,
            "status": "succeeded",
            "package_manifest_ref": package_manifest_ref,
            "artifact_refs": [source_ref],
            "evidence_refs": [assertion_evidence, holdout_evidence, recovery_evidence, *e2e_refs],
            "tool_gaps": [],
            "skill_usage_receipt_ref": skill_receipt,
            "failure": None,
        }
    )
    creation_result = CreationResultV1.model_validate(creation_payload)

    all_content: dict[str, bytes] = {
        str(source_ref["uri"]): archive,
        str(package_manifest_ref["uri"]): package_bytes,
        str(skill_receipt["uri"]): b'{"skill_usage":"accepted"}',
        str(assertion_evidence["uri"]): b'{"assertion":"passed"}',
        str(holdout_evidence["uri"]): b'{"holdout":"passed"}',
        str(recovery_evidence["uri"]): b'{"recovery":"passed"}',
    }
    all_content.update(
        {
            str(item["reference"]["uri"]): files[str(item["path"])]
            for item in artifacts
        }
    )
    all_content.update(
        {
            str(reference["uri"]): f'{{"e2e":{index}}}'.encode()
            for index, reference in enumerate(e2e_refs, start=1)
        }
    )
    return ValidationCase(
        job=job,
        creation_result=creation_result,
        package=package,
        store=RecordingContentStore(all_content),
        files=files,
    )


@pytest.mark.asyncio
async def test_validator_independently_accepts_a_bound_digest_verified_package() -> None:
    case = _validation_case()

    validated = await CapabilityPackageValidator(content_store=case.store).validate(
        job=case.job,
        creation_result=case.creation_result,
        package=case.package,
    )

    assert validated is case.package
    expected_uris = {
        reference.uri
        for reference in (
            case.creation_result.package_manifest_ref,
            *case.creation_result.artifact_refs,
            *case.creation_result.evidence_refs,
            case.creation_result.skill_usage_receipt_ref,
            case.package.source_ref,
            case.package.team_manifest_ref,
            *(item.reference for item in case.package.artifacts),
            *(ref for item in case.package.assertion_outcomes for ref in item.evidence_refs),
            *(item.evidence_ref for item in case.package.private_holdout_receipts),
            case.package.recovery_receipt.evidence_ref,
            *case.package.release_evidence_refs,
            case.package.skill_usage_receipt_ref,
            case.package.runbook_ref,
        )
        if reference is not None
    }
    assert set(case.store.reads) == expected_uris


@pytest.mark.asyncio
async def test_validator_does_not_trust_forge_succeeded_when_content_digest_is_wrong() -> None:
    case = _validation_case()
    case.store.content_by_uri[case.package.source_ref.uri] += b"tampered"

    with pytest.raises(CapabilityPackageValidationError, match="digest"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "updates", "message"),
    (
        ("package", {"factory_job_id": "99999999-9999-4999-8999-999999999999"}, "factory job"),
        ("package", {"correlation_id": "99999999-9999-4999-8999-999999999999"}, "correlation"),
        ("package", {"subject_version": 2}, "subject version"),
        ("package", {"capability_id": "foreign_capability"}, "capability"),
        ("creation", {"creation_job_id": "99999999-9999-4999-8999-999999999999"}, "creation job"),
        ("creation", {"correlation_id": "99999999-9999-4999-8999-999999999999"}, "correlation"),
        ("creation", {"subject_version": 2}, "subject version"),
    ),
)
async def test_validator_rejects_foreign_job_relationships(
    target: str,
    updates: dict[str, object],
    message: str,
) -> None:
    case = _validation_case()
    package = case.package
    creation_result = case.creation_result
    if target == "package":
        package = package.model_copy(update=updates)
    else:
        creation_result = creation_result.model_copy(update=updates)

    with pytest.raises(CapabilityPackageValidationError, match=message):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=creation_result,
            package=package,
        )


@pytest.mark.asyncio
async def test_validator_requires_exact_captain_assertion_and_holdout_receipts() -> None:
    case = _validation_case()
    package = case.package.model_copy(
        update={
            "assertion_outcomes": (),
            "private_holdout_receipts": (),
        }
    )

    with pytest.raises(CapabilityPackageValidationError, match="acceptance assertions"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=package,
        )


@pytest.mark.asyncio
async def test_validator_requires_creation_skill_receipt_and_source_archive_bindings() -> None:
    case = _validation_case()
    creation_result = case.creation_result.model_copy(
        update={"artifact_refs": (), "skill_usage_receipt_ref": None}
    )

    with pytest.raises(CapabilityPackageValidationError, match="source archive|skill usage"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
async def test_validator_rejects_manifest_bytes_that_do_not_describe_the_package() -> None:
    case = _validation_case()
    manifest_ref = case.creation_result.package_manifest_ref
    assert manifest_ref is not None
    mismatched = case.package.model_copy(update={"capability_version": 2})
    mismatched_bytes = mismatched.model_dump_json(by_alias=True).encode("utf-8")
    mismatched_ref = manifest_ref.model_copy(
        update={"sha256": hashlib.sha256(mismatched_bytes).hexdigest()}
    )
    case.store.content_by_uri[mismatched_ref.uri] = mismatched_bytes
    creation_result = case.creation_result.model_copy(
        update={"package_manifest_ref": mismatched_ref}
    )

    with pytest.raises(CapabilityPackageValidationError, match="manifest.*package"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_entry",
    ("../escaped.py", "/absolute.py", "C:/escaped.py"),
)
async def test_validator_rejects_archive_path_traversal(unsafe_entry: str) -> None:
    case = _validation_case()
    unsafe_files = {**case.files, unsafe_entry: b"ESCAPED = True\n"}
    await _replace_source_archive(case, _zip_bytes(unsafe_files))

    with pytest.raises(CapabilityPackageValidationError, match="unsafe path"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
async def test_validator_rejects_archive_symlinks() -> None:
    case = _validation_case()
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for path, content in case.files.items():
            archive.writestr(path, content)
        link = zipfile.ZipInfo("autogen/escape.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(link, "../../outside.py")
    await _replace_source_archive(case, output.getvalue())

    with pytest.raises(CapabilityPackageValidationError, match="symlink"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
async def test_validator_rejects_undeclared_archive_members() -> None:
    case = _validation_case()
    archive = _zip_bytes({**case.files, "autogen/undeclared.py": b"UNSEALED = True\n"})
    await _replace_source_archive(case, archive)

    with pytest.raises(CapabilityPackageValidationError, match="declared artifacts"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
async def test_validator_rejects_team_manifest_schema_or_capability_mismatch() -> None:
    case = _validation_case(team_manifest_updates={"capability_id": "foreign_capability"})

    with pytest.raises(CapabilityPackageValidationError, match="team manifest capability"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
async def test_validator_imports_autogen_code_in_the_fresh_workspace() -> None:
    case = _validation_case(autogen_source=b"raise RuntimeError('must import')\n")

    with pytest.raises(CapabilityPackageValidationError, match="AutoGen import"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
async def test_validator_runs_only_manifest_allowlisted_package_tests() -> None:
    case = _validation_case(package_test=b"def test_generated_failure():\n    assert False\n")

    with pytest.raises(CapabilityPackageValidationError, match="package tests"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=case.package,
        )


@pytest.mark.asyncio
async def test_validator_rejects_test_paths_not_exactly_declared_as_test_artifacts() -> None:
    case = _validation_case(
        team_manifest_updates={"test_paths": ["tests/not-sealed.py"]}
    )

    with pytest.raises(CapabilityPackageValidationError, match="allowlisted test paths"):
        await CapabilityPackageValidator(content_store=case.store).validate(
            job=case.job,
            creation_result=case.creation_result,
            package=case.package,
        )


async def _replace_source_archive(case: ValidationCase, archive: bytes) -> None:
    """Mutate only test models/store to preserve a valid digest binding."""

    source_ref = case.package.source_ref.model_copy(
        update={"sha256": hashlib.sha256(archive).hexdigest()}
    )
    object.__setattr__(case, "package", case.package.model_copy(update={"source_ref": source_ref}))
    creation_source_ref = case.creation_result.artifact_refs[0].model_copy(
        update={"sha256": source_ref.sha256}
    )
    object.__setattr__(
        case,
        "creation_result",
        case.creation_result.model_copy(update={"artifact_refs": (creation_source_ref,)}),
    )
    case.store.content_by_uri[source_ref.uri] = archive
    package_bytes = case.package.model_dump_json(by_alias=True).encode("utf-8")
    manifest_ref = case.creation_result.package_manifest_ref
    assert manifest_ref is not None
    updated_manifest_ref = manifest_ref.model_copy(
        update={"sha256": hashlib.sha256(package_bytes).hexdigest()}
    )
    object.__setattr__(
        case,
        "creation_result",
        case.creation_result.model_copy(
            update={"package_manifest_ref": updated_manifest_ref}
        ),
    )
    case.store.content_by_uri[updated_manifest_ref.uri] = package_bytes
