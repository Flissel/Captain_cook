from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from agenten.agent_factory.forge_contracts import (
    CreationJobV1 as CaptainCreationJob,
    CreationPackageManifestV1 as CaptainCreationPackageManifest,
    CreationResultV1 as CaptainCreationResult,
    FactoryBuildAssignmentV1 as CaptainFactoryBuildAssignment,
    ForgeBuildSkillUsageReceiptV1 as CaptainForgeBuildSkillUsageReceipt,
    CreationJobV3 as CaptainCreationJobV3,
    CreationResultV2 as CaptainCreationResultV2,
    FactoryActionRequestV1 as CaptainFactoryActionRequestV1,
    FactoryActionResponseV1 as CaptainFactoryActionResponseV1,
    FactoryBuildAssignmentV2 as CaptainFactoryBuildAssignmentV2,
    FactoryBuildEffectV2 as CaptainFactoryBuildEffectV2,
    validate_action_response_binding as captain_validate_action_response_binding,
    validate_build_effect_binding as captain_validate_build_effect_binding,
)
from minibook.swarm.contracts import (
    CreationJobV1,
    CreationPackageManifestV1,
    CreationResultV1,
    FactoryBuildAssignmentV1,
    ForgeBuildSkillUsageReceiptV1,
    CreationJobV3,
    CreationResultV2,
    FactoryActionRequestV1,
    FactoryActionResponseV1,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
    validate_action_response_binding,
    validate_build_effect_binding,
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
        "skill_usage_receipt_ref": {
            "uri": "artifact://forge/skill-receipt/" + "f" * 64,
            "sha256": "f" * 64,
            "media_type": "application/json",
        },
    }
    parent = CaptainCreationPackageManifest.model_validate(payload)
    minibook = CreationPackageManifestV1.model_validate(payload)

    assert parent.model_dump(mode="json", by_alias=True) == minibook.model_dump(
        mode="json", by_alias=True
    )


def test_parent_and_minibook_validate_the_same_forge_build_skill_receipt() -> None:
    payload = {
        "schema": "hermes.forge-build-skill-usage-receipt.v1",
        "producer": "hermes",
        "outcome": "fulfilled",
        "creation_job_id": "00000000-0000-0000-0000-000000000401",
        "factory_job_id": "00000000-0000-0000-0000-000000000301",
        "correlation_id": "00000000-0000-0000-0000-000000000302",
        "subject_version": 1,
        "attempt": 1,
        "idempotency_key": "a" * 64,
        "released_skill": {
            "skill_id": "captain-factory-brief-codex",
            "version": 1,
            "content_ref": {
                "uri": "artifact://forge/skill/" + "b" * 64,
                "sha256": "b" * 64,
                "media_type": "text/markdown",
            },
            "content_sha256": "b" * 64,
        },
        "public_assertion_ids": ["assertion.team-starts"],
        "evidence_refs": [
            {
                "uri": "artifact://forge/evidence/" + "c" * 64,
                "sha256": "c" * 64,
                "media_type": "application/json",
            }
        ],
    }
    parent = CaptainForgeBuildSkillUsageReceipt.model_validate(payload)
    minibook = ForgeBuildSkillUsageReceiptV1.model_validate(payload)

    assert parent.model_dump(mode="json", by_alias=True) == minibook.model_dump(
        mode="json", by_alias=True
    )


@pytest.mark.parametrize(
    ("fixture_name", "parent_type", "minibook_type"),
    (
        ("minibook_creation_job.v3.json", CaptainCreationJobV3, CreationJobV3),
        ("hermes_factory_assignment.v2.json", CaptainFactoryBuildAssignmentV2, FactoryBuildAssignmentV2),
        ("hermes_factory_build_effect.v2.json", CaptainFactoryBuildEffectV2, FactoryBuildEffectV2),
        ("minibook_creation_result.v2.json", CaptainCreationResultV2, CreationResultV2),
        ("factory_action_request.v1.json", CaptainFactoryActionRequestV1, FactoryActionRequestV1),
        ("factory_action_response.v1.json", CaptainFactoryActionResponseV1, FactoryActionResponseV1),
    ),
)
def test_v3_contract_fixture_is_byte_compatible(
    fixture_name: str, parent_type: type, minibook_type: type
) -> None:
    _assert_byte_compatible(fixture_name, parent_type, minibook_type)


@pytest.mark.parametrize("contract", (CaptainCreationResultV2, CreationResultV2))
def test_v3_result_rejects_duplicate_artifact_references(contract: type) -> None:
    payload = json.loads((FIXTURES / "minibook_creation_result.v2.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="distinct"):
        contract.model_validate(payload | {"candidate_manifest_ref": payload["team_manifest_ref"]})


@pytest.mark.parametrize("contract", (CaptainFactoryBuildAssignmentV2, FactoryBuildAssignmentV2))
def test_assignment_rejects_unsafe_command_and_extra_field(contract: type) -> None:
    payload = json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="allowed command"):
        contract.model_validate(payload | {"allowed_commands": [["sh", "-c", "echo *"]]})
    with pytest.raises(ValidationError):
        contract.model_validate(payload | {"extra": "forbidden"})


@pytest.mark.parametrize("contract", (CaptainFactoryActionRequestV1, FactoryActionRequestV1))
def test_action_request_rejects_private_display_content(contract: type) -> None:
    payload = json.loads((FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="display-safe"):
        contract.model_validate(payload | {"message": "system prompt: private"})


@pytest.mark.parametrize("contract", (CaptainCreationJobV3, CreationJobV3))
def test_v3_job_rejects_missing_output_contract_and_lease_identity_drift(contract: type) -> None:
    payload = json.loads((FIXTURES / "minibook_creation_job.v3.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError):
        contract.model_validate({key: value for key, value in payload.items() if key != "required_output_contract"})
    with pytest.raises(ValidationError, match="lease digest"):
        contract.model_validate(payload | {"lease_ref": payload["lease_ref"] | {"sha256": "0" * 64}})


@pytest.mark.parametrize("contract", (CaptainFactoryActionResponseV1, FactoryActionResponseV1))
def test_action_response_rejects_neutral_secret_value_and_unsafe_scope(contract: type) -> None:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="secret-like"):
        contract.model_validate(payload | {"answer_metadata": {"note": "Basic dXNlcjpwYXNz"}})
    with pytest.raises(ValidationError, match="display-safe"):
        contract.model_validate(payload | {"approved_scope": "api_key=abcdef"})


@pytest.mark.parametrize("contract", (CaptainFactoryActionResponseV1, FactoryActionResponseV1))
def test_action_response_rejects_common_neutral_key_secret_formats(contract: type) -> None:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    for value in ("sk_test_abcdefghijklmnopqrstuvwxyz012345", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature", "ghp_abcdefghijklmnopqrstuvwxyz0123456789"):
        with pytest.raises(ValidationError, match="secret-like"):
            contract.model_validate(payload | {"answer_metadata": {"note": value}})
    assert contract.model_validate(payload | {"answer_metadata": {"digest": "a" * 64}}).answer_metadata["digest"] == "a" * 64


@pytest.mark.parametrize("contract", (CaptainFactoryBuildAssignmentV2, FactoryBuildAssignmentV2))
def test_assignment_uses_exact_command_allowlist(contract: type) -> None:
    payload = json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8"))
    for command in (["py.exe", "-c", "print(1)"], ["python3.12", "-c", "print(1)"], ["wsl.exe", "pytest"], ["python", "-m", "subprocess", "pytest"]):
        with pytest.raises(ValidationError, match="allowed command"):
            contract.model_validate(payload | {"allowed_commands": [command]})
    assert contract.model_validate(payload | {"allowed_commands": [["python.exe", "-m", "pytest"]]}).assignment_id


@pytest.mark.parametrize(
    ("assignment_type", "effect_type", "validator"),
    ((CaptainFactoryBuildAssignmentV2, CaptainFactoryBuildEffectV2, captain_validate_build_effect_binding), (FactoryBuildAssignmentV2, FactoryBuildEffectV2, validate_build_effect_binding)),
)
def test_build_effect_binding_rejects_factory_job_identity_drift(assignment_type: type, effect_type: type, validator: object) -> None:
    assignment = assignment_type.model_validate(json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8")))
    effect_payload = json.loads((FIXTURES / "hermes_factory_build_effect.v2.json").read_text(encoding="utf-8"))
    effect = effect_type.model_validate(effect_payload | {"factory_job_id": "00000000-0000-0000-0000-000000000399"})
    with pytest.raises(ValueError, match="identity drift"):
        validator(assignment, effect)  # type: ignore[operator]


@pytest.mark.parametrize("contract", (CaptainFactoryActionResponseV1, FactoryActionResponseV1))
def test_metadata_rejects_opaque_secret_tokens_but_allows_long_prose(contract: type) -> None:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    prose = "A display-safe sentence with normal language and spaces. " * 4
    assert contract.model_validate(payload | {"answer_metadata": {"note": prose}}).answer_metadata["note"] == prose
    with pytest.raises(ValidationError, match="secret-like"):
        contract.model_validate(payload | {"answer_metadata": {"note": "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk"}})
    with pytest.raises(ValidationError, match="secret-like"):
        contract.model_validate(payload | {"answer_metadata": {"note": "a" * 64}})
    assert contract.model_validate(payload | {"answer_metadata": {"sha256": "a" * 64}}).answer_metadata["sha256"] == "a" * 64
    for value in ("A" * 64, "a" * 32, "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk"):
        with pytest.raises(ValidationError, match="secret-like"):
            contract.model_validate(payload | {"answer_metadata": {"sha256": value}})
    assert contract.model_validate(payload | {"answer_metadata": {"run_id": "2f1d8c50-4a7a-4d94-91fb-77a1ed58ba23"}}).answer_metadata["run_id"]


@pytest.mark.parametrize("contract", (CaptainFactoryBuildAssignmentV2, FactoryBuildAssignmentV2))
def test_assignment_rejects_relative_command_components(contract: type) -> None:
    payload = json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="allowed command"):
        contract.model_validate(payload | {"allowed_commands": [["python", "-m", "pytest", "."]]})
    for command in (["pytest", "--pythonpath", "x"], ["python", "-m", "compileall", "output", "-q"]):
        with pytest.raises(ValidationError, match="allowed command"):
            contract.model_validate(payload | {"allowed_commands": [command]})


@pytest.mark.parametrize("contract", (CaptainFactoryBuildAssignmentV2, FactoryBuildAssignmentV2))
def test_assignment_rejects_invalid_agents_unbounded_roots_and_non_utc_deadlines(contract: type) -> None:
    payload = json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8"))
    for override in ({"required_agent_ids": ["bad agent"]}, {"allowed_output_roots": [""]}, {"allowed_output_roots": ["."]}, {"deadline_at": "2026-08-09T12:00:00"}):
        with pytest.raises(ValidationError):
            contract.model_validate(payload | override)


@pytest.mark.parametrize(
    ("request_type", "response_type", "validator"),
    ((CaptainFactoryActionRequestV1, CaptainFactoryActionResponseV1, captain_validate_action_response_binding), (FactoryActionRequestV1, FactoryActionResponseV1, validate_action_response_binding)),
)
def test_action_response_binding_enforces_decision_and_scope(request_type: type, response_type: type, validator: object) -> None:
    request_payload = json.loads((FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")) | {"allowed_decisions": ["deny"]}
    response_payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    request = request_type.model_validate(request_payload)
    with pytest.raises(ValueError, match="decision"):
        validator(request, response_type.model_validate(response_payload))  # type: ignore[operator]
    with pytest.raises(ValueError, match="scope"):
        validator(request_type.model_validate(request_payload | {"allowed_decisions": ["approve"]}), response_type.model_validate(response_payload | {"approved_scope": "n8n.crm.write"}))  # type: ignore[operator]


@pytest.mark.parametrize("contract", (CaptainFactoryActionRequestV1, FactoryActionRequestV1))
def test_action_request_requires_utc_deadline(contract: type) -> None:
    payload = json.loads((FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="deadline_at must be UTC"):
        contract.model_validate(payload | {"deadline_at": "2026-08-09T12:00:00"})


@pytest.mark.parametrize("contract", (CaptainFactoryActionRequestV1, FactoryActionRequestV1))
def test_action_request_exposes_authoritative_denied_status(contract: type) -> None:
    payload = json.loads((FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8"))
    assert contract.model_validate(payload | {"status": "denied"}).status == "denied"


@pytest.mark.parametrize("contract", (CaptainFactoryActionResponseV1, FactoryActionResponseV1))
def test_action_response_requires_utc_timestamp(contract: type) -> None:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="answered_at must be UTC"):
        contract.model_validate(payload | {"answered_at": "2026-08-08T12:00:00"})
