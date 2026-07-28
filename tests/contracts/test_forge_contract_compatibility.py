from __future__ import annotations

import json
from pathlib import Path

from agenten.agent_factory.forge_contracts import (
    CreationJobV1 as CaptainCreationJob,
    CreationPackageManifestV1 as CaptainCreationPackageManifest,
    CreationResultV1 as CaptainCreationResult,
    FactoryBuildAssignmentV1 as CaptainFactoryBuildAssignment,
)
from minibook.swarm.contracts import (
    CreationJobV1,
    CreationPackageManifestV1,
    CreationResultV1,
    FactoryBuildAssignmentV1,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


def _assert_byte_compatible(name: str, parent_type: type, minibook_type: type) -> None:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    parent = parent_type.model_validate(payload)
    minibook = minibook_type.model_validate(payload)
    assert parent.model_dump(mode="json", by_alias=True) == minibook.model_dump(
        mode="json", by_alias=True
    )


def test_parent_and_minibook_validate_the_same_creation_job_fixture() -> None:
    _assert_byte_compatible(
        "minibook_creation_job.v1.json", CaptainCreationJob, CreationJobV1
    )


def test_parent_and_minibook_validate_the_same_creation_result_fixture() -> None:
    _assert_byte_compatible(
        "minibook_creation_result.v1.json", CaptainCreationResult, CreationResultV1
    )


def test_parent_and_minibook_validate_the_same_assignment_fixture() -> None:
    _assert_byte_compatible(
        "hermes_factory_assignment.v1.json",
        CaptainFactoryBuildAssignment,
        FactoryBuildAssignmentV1,
    )


def test_parent_and_minibook_validate_the_same_creation_package_manifest() -> None:
    payload = {
        "schema": "minibook.creation-package-manifest.v1",
        "creation_job_id": "00000000-0000-0000-0000-000000000401",
        "factory_job_id": "00000000-0000-0000-0000-000000000301",
        "correlation_id": "00000000-0000-0000-0000-000000000302",
        "subject_version": 1,
        "attempt": 1,
        "candidate_manifest_ref": {
            "uri": "artifact://forge/candidate/" + "d" * 64,
            "sha256": "d" * 64,
            "media_type": "application/json",
        },
        "source_archive_ref": {
            "uri": "artifact://forge/source/" + "e" * 64,
            "sha256": "e" * 64,
            "media_type": "application/zip",
        },
    }
    parent = CaptainCreationPackageManifest.model_validate(payload)
    minibook = CreationPackageManifestV1.model_validate(payload)

    assert parent.model_dump(mode="json", by_alias=True) == minibook.model_dump(
        mode="json", by_alias=True
    )
