from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from minibook.swarm.contracts import (
    CreationJobV1,
    CreationJobV3,
    CreationResultV1,
    CreationResultV2,
    FactoryActionRequestV1,
    FactoryActionResponseV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)


FIXTURES = Path(__file__).parents[2] / "tests" / "fixtures" / "contracts"


def job_payload() -> dict[str, object]:
    return json.loads(
        (FIXTURES / "minibook_creation_job.v1.json").read_text(encoding="utf-8")
    )


@pytest.mark.parametrize(
    "field", ["holdout_bodies", "credentials", "absolute_workspace"]
)
def test_creation_job_rejects_private_or_unrestricted_fields(field: str) -> None:
    with pytest.raises(ValidationError):
        CreationJobV1.model_validate(job_payload() | {field: "forbidden"})


@pytest.mark.parametrize("uri", ["C:/work/result.json", "/tmp/result.json", "../result"])
def test_creation_job_rejects_non_opaque_artifact_uris(uri: str) -> None:
    payload = job_payload()
    payload["input_ref"] = payload["input_ref"] | {"uri": uri}  # type: ignore[operator]
    with pytest.raises(ValidationError):
        CreationJobV1.model_validate(payload)


def test_success_requires_manifest_and_skill_receipt() -> None:
    payload = json.loads(
        (FIXTURES / "minibook_creation_result.v1.json").read_text(encoding="utf-8")
    )
    payload["package_manifest_ref"] = None
    with pytest.raises(ValidationError, match="package manifest"):
        CreationResultV1.model_validate(payload)


def test_failure_requires_sanitized_failure() -> None:
    payload = json.loads(
        (FIXTURES / "minibook_creation_result.v1.json").read_text(encoding="utf-8")
    )
    payload.update(status="failed", package_manifest_ref=None, skill_usage_receipt_ref=None)
    with pytest.raises(ValidationError, match="failure"):
        CreationResultV1.model_validate(payload)


@pytest.mark.parametrize(
    ("fixture_name", "contract"),
    (
        ("minibook_creation_job.v3.json", CreationJobV3),
        ("hermes_factory_assignment.v2.json", FactoryBuildAssignmentV2),
        ("hermes_factory_build_effect.v2.json", FactoryBuildEffectV2),
        ("minibook_creation_result.v2.json", CreationResultV2),
        ("factory_action_request.v1.json", FactoryActionRequestV1),
        ("factory_action_response.v1.json", FactoryActionResponseV1),
    ),
)
def test_v3_contract_fixture_is_canonical(fixture_name: str, contract: type) -> None:
    payload = json.loads((FIXTURES / fixture_name).read_text(encoding="utf-8"))
    assert contract.model_validate(payload).model_dump(mode="json", by_alias=True) == payload


def test_creation_job_v3_rejects_source_archive() -> None:
    payload = json.loads((FIXTURES / "minibook_creation_job.v3.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        CreationJobV3.model_validate(payload | {"source_archive_ref": payload["lease_ref"]})


def test_creation_job_v3_rejects_mismatched_lease_digest() -> None:
    payload = json.loads((FIXTURES / "minibook_creation_job.v3.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="lease digest"):
        CreationJobV3.model_validate(payload | {"lease_sha256": "0" * 64})


def test_creation_job_v3_rejects_duplicate_agents_and_invalid_handoffs() -> None:
    payload = json.loads((FIXTURES / "minibook_creation_job.v3.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="agent ids"):
        CreationJobV3.model_validate(payload | {"required_agent_ids": ["planner", "planner"]})
    bad_handoff = payload["ordered_handoffs"][0] | {"to_agent_id": "missing"}
    with pytest.raises(ValidationError, match="handoff"):
        CreationJobV3.model_validate(payload | {"ordered_handoffs": [bad_handoff]})


def test_creation_job_v3_rejects_absolute_path_and_raw_credential() -> None:
    payload = json.loads((FIXTURES / "minibook_creation_job.v3.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        CreationJobV3.model_validate(payload | {"workspace_path": "C:/private/work"})
    with pytest.raises(ValidationError):
        CreationJobV3.model_validate(payload | {"api_key": "not-a-real-secret"})


def test_creation_result_v2_cannot_succeed_from_receipt_only() -> None:
    payload = json.loads((FIXTURES / "minibook_creation_result.v2.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="successful V2 result requires"):
        CreationResultV2.model_validate(payload | {"team_manifest_ref": None})


def test_factory_action_response_rejects_raw_credentials() -> None:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="credential_ref"):
        FactoryActionResponseV1.model_validate(payload | {"credential_ref": "raw-secret"})
    with pytest.raises(ValidationError, match="secret-like"):
        FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"api_key": "raw-secret"}})


def test_factory_effect_rejects_bad_archive_media_type() -> None:
    payload = json.loads((FIXTURES / "hermes_factory_build_effect.v2.json").read_text(encoding="utf-8"))
    bad_archive = payload["source_archive_ref"] | {"media_type": "application/json"}
    with pytest.raises(ValidationError, match="application/zip"):
        FactoryBuildEffectV2.model_validate(payload | {"source_archive_ref": bad_archive})


def test_v3_result_rejects_duplicate_references_and_extra_fields() -> None:
    payload = json.loads((FIXTURES / "minibook_creation_result.v2.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="distinct"):
        CreationResultV2.model_validate(payload | {"candidate_manifest_ref": payload["team_manifest_ref"]})
    with pytest.raises(ValidationError):
        CreationResultV2.model_validate(payload | {"unexpected": "forbidden"})


def test_assignment_rejects_absolute_output_root_and_unsafe_command() -> None:
    payload = json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="output roots"):
        FactoryBuildAssignmentV2.model_validate(payload | {"allowed_output_roots": ["C:/private"]})
    with pytest.raises(ValidationError, match="allowed command"):
        FactoryBuildAssignmentV2.model_validate(payload | {"allowed_commands": [["sh", "-c", "rm *"]]})


def test_action_contracts_reject_private_content_and_identity_drift() -> None:
    request = json.loads((FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8"))
    response = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="display-safe"):
        FactoryActionRequestV1.model_validate(request | {"message": "prompt: hidden system prompt"})
    with pytest.raises(ValidationError, match="checkpoint digest"):
        FactoryActionRequestV1.model_validate(request | {"safe_checkpoint_sha256": "0" * 64})
    with pytest.raises(ValidationError, match="secret-like"):
        FactoryActionResponseV1.model_validate(response | {"answer_metadata": {"nested": {"provider_response": "private"}}})
