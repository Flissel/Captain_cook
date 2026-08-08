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
    validate_action_response_binding,
    validate_build_effect_binding,
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


def test_v3_job_requires_output_contract_and_binds_lease_identity() -> None:
    payload = json.loads((FIXTURES / "minibook_creation_job.v3.json").read_text(encoding="utf-8"))
    missing = {key: value for key, value in payload.items() if key != "required_output_contract"}
    with pytest.raises(ValidationError):
        CreationJobV3.model_validate(missing)
    drifted_lease = payload["lease_ref"] | {"sha256": "0" * 64}
    with pytest.raises(ValidationError, match="lease digest"):
        CreationJobV3.model_validate(payload | {"lease_ref": drifted_lease})


def test_action_contracts_reject_neutral_secret_values_and_unsafe_scope() -> None:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    with pytest.raises(ValidationError, match="secret-like"):
        FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"note": "Bearer abcdefghijklmnopqrstuvwxyz0123456789"}})
    with pytest.raises(ValidationError, match="display-safe"):
        FactoryActionResponseV1.model_validate(payload | {"approved_scope": "token=abcdefghijk"})


def test_assignment_rejects_command_bypasses_and_v3_models_are_frozen() -> None:
    assignment = json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8"))
    for command in (["PowerShell.exe", "-Command", "Get-ChildItem"], ["PYTHON.EXE", "-c", "print(1)"], ["python", "../tool.py"]):
        with pytest.raises(ValidationError, match="allowed command"):
            FactoryBuildAssignmentV2.model_validate(assignment | {"allowed_commands": [command]})
    job = CreationJobV3.model_validate(json.loads((FIXTURES / "minibook_creation_job.v3.json").read_text(encoding="utf-8")))
    with pytest.raises(ValidationError, match="frozen_instance"):
        job.attempt = 2  # type: ignore[misc]


def test_action_metadata_rejects_secret_formats_and_accepts_safe_identifiers() -> None:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    for value in ("sk_live_abcdefghijklmnopqrstuvwxyz012345", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.signature", "AKIAABCDEFGHIJKLMNOP"):
        with pytest.raises(ValidationError, match="secret-like"):
            FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"note": value}})
    accepted = FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"digest": "a" * 64, "run_id": "00000000-0000-0000-0000-000000000001"}})
    assert accepted.answer_metadata["digest"] == "a" * 64


def test_assignment_allows_only_bounded_python_commands() -> None:
    payload = json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8"))
    for command in (["py.exe", "-c", "print(1)"], ["python3.12", "-c", "print(1)"], ["wsl.exe", "pytest"], ["python", "-m", "subprocess", "pytest"]):
        with pytest.raises(ValidationError, match="allowed command"):
            FactoryBuildAssignmentV2.model_validate(payload | {"allowed_commands": [command]})
    assert FactoryBuildAssignmentV2.model_validate(payload | {"allowed_commands": [["python.exe", "-m", "compileall", "output"]]}).assignment_id


def test_cross_contract_binding_rejects_identity_drift() -> None:
    assignment = FactoryBuildAssignmentV2.model_validate(json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8")))
    effect_payload = json.loads((FIXTURES / "hermes_factory_build_effect.v2.json").read_text(encoding="utf-8"))
    effect = FactoryBuildEffectV2.model_validate(effect_payload | {"factory_job_id": "00000000-0000-0000-0000-000000000399"})
    with pytest.raises(ValueError, match="identity drift"):
        validate_build_effect_binding(assignment, effect)
    request = FactoryActionRequestV1.model_validate(json.loads((FIXTURES / "factory_action_request.v1.json").read_text(encoding="utf-8")))
    response = FactoryActionResponseV1.model_validate(json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8")) | {"request_id": "00000000-0000-0000-0000-000000000699"})
    with pytest.raises(ValueError, match="identity drift"):
        validate_action_response_binding(request, response)


def test_metadata_uses_token_secret_detection_without_rejecting_long_prose() -> None:
    payload = json.loads((FIXTURES / "factory_action_response.v1.json").read_text(encoding="utf-8"))
    prose = "This is a display-safe explanation with ordinary words and spaces. " * 4
    assert FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"note": prose}}).answer_metadata["note"] == prose
    with pytest.raises(ValidationError, match="secret-like"):
        FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"note": "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk"}})
    with pytest.raises(ValidationError, match="secret-like"):
        FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"note": "a" * 64}})
    assert FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"sha256": "a" * 64}}).answer_metadata["sha256"] == "a" * 64
    for value in ("A" * 64, "a" * 32, "Y7pL2vQ9xN4mR8sT6uW3aB5cD1eF0gHk"):
        with pytest.raises(ValidationError, match="secret-like"):
            FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"sha256": value}})
    assert FactoryActionResponseV1.model_validate(payload | {"answer_metadata": {"run_id": "2f1d8c50-4a7a-4d94-91fb-77a1ed58ba23"}}).answer_metadata["run_id"]


def test_assignment_rejects_pathful_executables() -> None:
    payload = json.loads((FIXTURES / "hermes_factory_assignment.v2.json").read_text(encoding="utf-8"))
    for command in (["tools/python.exe", "-m", "pytest"], ["tools\\python.exe", "-m", "pytest"], ["./python", "-m", "pytest"], ["tools/pytest.cmd"]):
        with pytest.raises(ValidationError, match="allowed command"):
            FactoryBuildAssignmentV2.model_validate(payload | {"allowed_commands": [command]})
    with pytest.raises(ValidationError, match="allowed command"):
        FactoryBuildAssignmentV2.model_validate(payload | {"allowed_commands": [["python", "-m", "pytest", "."]]})
    for command in (["pytest", "--pythonpath", "x"], ["python", "-m", "compileall", "output", "-q"]):
        with pytest.raises(ValidationError, match="allowed command"):
            FactoryBuildAssignmentV2.model_validate(payload | {"allowed_commands": [command]})
