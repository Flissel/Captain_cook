from __future__ import annotations

import json
from pathlib import Path

from agenten.agent_factory.forge_contracts import (
    CreationJobV1 as CaptainCreationJob,
    CreationResultV1 as CaptainCreationResult,
    FactoryBuildAssignmentV1 as CaptainFactoryBuildAssignment,
)
from minibook.swarm.contracts import (
    CreationJobV1,
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
