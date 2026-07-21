from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from agenten.agent_factory.outcome_contracts import (
    CapabilityPackageManifestV1,
    ExecutionOutcomeV1,
    FactoryTerminalDecision,
    FactoryTerminalState,
    validate_execution_outcome_binding,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    RuntimeOperation,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _artifact(path: str, digest: str, *, kind: str = "evidence") -> dict[str, object]:
    return {
        "path": path,
        "kind": kind,
        "reference": {
            "uri": f"artifact://capability-package/{digest}",
            "sha256": digest,
            "media_type": "application/octet-stream",
        },
    }


def _runtime_command() -> AgentRuntimeCommand:
    return AgentRuntimeCommand.model_validate(
        _fixture("agent_runtime_command.v1.json")
    )


def _runtime_result() -> AgentRuntimeResult:
    return AgentRuntimeResult.model_validate(_fixture("agent_runtime_result.v1.json"))


def _runtime_binding() -> tuple[
    ExecutionOutcomeV1,
    AgentRuntimeCommand,
    AgentRuntimeResult,
]:
    outcome = ExecutionOutcomeV1.model_validate(
        _fixture("execution_outcome.v1.json")
    )
    command = _runtime_command()
    result = _runtime_result()
    command = command.model_copy(
        update={
            "event_id": outcome.command_id,
            "correlation_id": outcome.correlation_id,
        }
    )
    result = result.model_copy(
        update={
            "event_id": outcome.result_id,
            "command_id": outcome.command_id,
            "correlation_id": outcome.correlation_id,
        }
    )
    return outcome, command, result


def test_capability_package_fixture_is_strict_frozen_and_round_trips() -> None:
    payload = _fixture("capability_package_manifest.v1.json")

    manifest = CapabilityPackageManifestV1.model_validate(payload)

    assert manifest.model_dump(mode="json", by_alias=True) == payload
    assert manifest.schema_name == "captain.capability-package.v1"
    with pytest.raises(ValidationError, match="frozen"):
        manifest.capability_version = 2
    with pytest.raises(ValidationError, match="extra"):
        CapabilityPackageManifestV1.model_validate({**payload, "forge_status": "succeeded"})


@pytest.mark.parametrize(
    "missing_root",
    ("team-manifest.json", "autogen/", "skills/", "tests/", "evidence/", "RUNBOOK.md"),
)
def test_package_requires_every_unconditional_logical_root(missing_root: str) -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    payload["artifacts"] = [
        item
        for item in payload["artifacts"]
        if not str(item["path"]).startswith(missing_root)
    ]

    with pytest.raises(ValidationError, match="logical package root"):
        CapabilityPackageManifestV1.model_validate(payload)


def test_n8n_root_is_required_only_for_declared_n8n_assertions() -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    payload["assertion_outcomes"][0]["integration_intent"] = "n8n"

    with pytest.raises(ValidationError, match="n8n/"):
        CapabilityPackageManifestV1.model_validate(payload)

    payload["artifacts"].append(
        _artifact("n8n/support-triage.v1.json", "b" * 64, kind="n8n_workflow")
    )
    assert CapabilityPackageManifestV1.model_validate(payload).artifacts[-1].path.startswith("n8n/")


def test_adapter_declaration_requires_an_adapter_root() -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    payload["artifacts"].append(
        _artifact("autogen/crm_adapter.py", "b" * 64, kind="local_adapter")
    )

    with pytest.raises(ValidationError, match="adapters/"):
        CapabilityPackageManifestV1.model_validate(payload)


@pytest.mark.parametrize("unsafe_path", ("../secret.txt", "/tmp/code.py", "C:\\work\\code.py", "autogen\\..\\secret.py"))
def test_package_rejects_unsafe_artifact_paths(unsafe_path: str) -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    payload["artifacts"][1]["path"] = unsafe_path

    with pytest.raises(ValidationError, match="safe relative|local path"):
        CapabilityPackageManifestV1.model_validate(payload)


def test_package_rejects_duplicate_paths_and_digests() -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    duplicate_path = {**payload["artifacts"][1], "reference": payload["artifacts"][2]["reference"]}
    payload["artifacts"].append(duplicate_path)
    with pytest.raises(ValidationError, match="paths must be unique"):
        CapabilityPackageManifestV1.model_validate(payload)

    payload = _fixture("capability_package_manifest.v1.json")
    duplicate_digest = {**payload["artifacts"][1], "path": "autogen/duplicate.py"}
    payload["artifacts"].append(duplicate_digest)
    with pytest.raises(ValidationError, match="digests must be unique"):
        CapabilityPackageManifestV1.model_validate(payload)


def test_package_rejects_raw_private_holdout_body() -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    payload["private_holdout_receipts"][0]["holdout_body"] = "private test instructions"

    with pytest.raises(ValidationError, match="private"):
        CapabilityPackageManifestV1.model_validate(payload)


@pytest.mark.parametrize(
    ("receipt", "assertion_id"),
    (
        ("private_holdout_receipts", "unknown-holdout-assertion"),
        ("recovery_receipt", "unknown-recovery-assertion"),
    ),
)
def test_package_rejects_receipts_for_unknown_assertions(
    receipt: str,
    assertion_id: str,
) -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    target = payload[receipt]
    if isinstance(target, list):
        target[0]["assertion_id"] = assertion_id
    else:
        target["assertion_id"] = assertion_id

    with pytest.raises(ValidationError, match="unknown assertion"):
        CapabilityPackageManifestV1.model_validate(payload)


def test_package_rejects_tool_gaps_for_unknown_assertions() -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    payload["tool_gaps"] = [
        {
            "schema": "TODO_TOOL.v1",
            "gap_id": "crm-lookup-gap",
            "severity": "optional",
            "input_contract_ref": {
                "uri": "artifact://tool-contracts/input",
                "sha256": "c" * 64,
                "media_type": "application/json",
            },
            "output_contract_ref": {
                "uri": "artifact://tool-contracts/output",
                "sha256": "d" * 64,
                "media_type": "application/json",
            },
            "least_privilege_capability": "crm.read",
            "implementation_options": [],
            "acceptance_assertion_ids": ["unknown-tool-assertion"],
            "evidence_ref": {
                "uri": "artifact://tool-gap/evidence",
                "sha256": "e" * 64,
                "media_type": "application/json",
            },
            "status": "resolved",
        }
    ]

    with pytest.raises(ValidationError, match="unknown assertion"):
        CapabilityPackageManifestV1.model_validate(payload)


def test_package_recursively_rejects_credentials_in_artifact_references() -> None:
    payload = _fixture("capability_package_manifest.v1.json")
    payload["release_evidence_refs"][0]["uri"] = (
        "artifact://release-evidence/token=not-redacted"
    )

    with pytest.raises(ValidationError, match="private"):
        CapabilityPackageManifestV1.model_validate(payload)


def test_execution_outcome_fixture_is_strict_frozen_and_round_trips() -> None:
    payload = _fixture("execution_outcome.v1.json")

    outcome = ExecutionOutcomeV1.model_validate(payload)

    assert outcome.model_dump(mode="json", by_alias=True) == payload
    assert outcome.status == "succeeded"
    with pytest.raises(ValidationError, match="frozen"):
        outcome.status = "failed"


@pytest.mark.parametrize(
    "business_output",
    (
        {"credentials": {"CRM_API_KEY": "not-redacted"}},
        {"notes": "authorization: Bearer abcdefghijklmnop"},
        {"holdout_body": "private case"},
        {"holdout": {"instructions": "private case"}},
        {"holdout_case": {"input": "private case"}},
        {"private_case": {"input": "private case"}},
        {"case_body": "private case"},
        {"transcript": "full model conversation"},
        {"output_path": "C:\\Users\\User\\workspace\\result.json"},
        {"notes": "read result from C:\\Users\\User\\workspace\\result.json"},
        {"output_path": "/home/runner/work/result.json"},
        {"output_path": "/etc/captain/config.json"},
        {"notes": "read result from /opt/captain/result.json"},
        {"output_path": "/workspace/result.json"},
        {"artifact": "file:///etc/captain/config.json"},
        {"artifact": "FILE://localhost/C:/captain/config.json"},
        {"artifact": "file:C:\\captain\\config.json"},
    ),
)
def test_execution_outcome_recursively_rejects_private_or_local_content(
    business_output: dict[str, object],
) -> None:
    payload = _fixture("execution_outcome.v1.json")
    payload["business_output"] = business_output

    with pytest.raises(ValidationError, match="private|local path"):
        ExecutionOutcomeV1.model_validate(payload)


def test_execution_outcome_requires_exactly_one_output_form() -> None:
    payload = _fixture("execution_outcome.v1.json")
    payload["output_ref"] = {
        "uri": "artifact://execution-output/" + "c" * 64,
        "sha256": "c" * 64,
        "media_type": "application/json",
    }
    with pytest.raises(ValidationError, match="exactly one"):
        ExecutionOutcomeV1.model_validate(payload)

    payload["business_output"] = None
    assert ExecutionOutcomeV1.model_validate(payload).output_ref is not None


def test_escalation_reference_is_bound_to_escalated_status() -> None:
    payload = _fixture("execution_outcome.v1.json")
    payload["status"] = "escalated"
    with pytest.raises(ValidationError, match="escalation_ref"):
        ExecutionOutcomeV1.model_validate(payload)

    payload["escalation_ref"] = {
        "uri": "artifact://execution-escalation/" + "d" * 64,
        "sha256": "d" * 64,
        "media_type": "application/json",
    }
    assert ExecutionOutcomeV1.model_validate(payload).status == "escalated"


def test_execution_outcome_binds_to_authoritative_runtime_command_and_result() -> None:
    outcome, command, result = _runtime_binding()

    assert outcome.capability_id != command.subject_id

    assert (
        validate_execution_outcome_binding(
            outcome,
            command=command,
            result=result,
            expected_capability_id=outcome.capability_id,
            expected_capability_version=outcome.capability_version,
            expected_team_version=outcome.team_version,
        )
        is outcome
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("capability_id", "different-capability", "capability identity"),
        ("capability_version", 4, "capability version"),
        ("team_version", 4, "team version"),
        ("correlation_id", UUID("00000000-0000-0000-0000-000000000099"), "correlation"),
        ("command_id", UUID("00000000-0000-0000-0000-000000000098"), "command"),
        ("result_id", UUID("00000000-0000-0000-0000-000000000097"), "result"),
    ),
)
def test_execution_outcome_rejects_unbound_runtime_identity(
    field: str,
    value: object,
    message: str,
) -> None:
    outcome, command, result = _runtime_binding()
    outcome = outcome.model_copy(update={field: value})

    with pytest.raises(ValueError, match=message):
        validate_execution_outcome_binding(
            outcome,
            command=command,
            result=result,
            expected_capability_id="support_triage",
            expected_capability_version=1,
            expected_team_version=1,
        )


@pytest.mark.parametrize(
    ("updates", "message"),
    (
        ({"command_id": UUID("00000000-0000-0000-0000-000000000098")}, "command"),
        ({"correlation_id": UUID("00000000-0000-0000-0000-000000000099")}, "correlation"),
        ({"subject_id": "different-capability"}, "subject"),
        ({"subject_version": 4}, "subject version"),
        ({"operation": RuntimeOperation.CODEX_RESUME}, "operation"),
    ),
)
def test_execution_outcome_rejects_result_not_bound_to_command(
    updates: dict[str, object],
    message: str,
) -> None:
    outcome, command, result = _runtime_binding()
    with pytest.raises(ValueError, match=message):
        validate_execution_outcome_binding(
            outcome,
            command=command,
            result=result.model_copy(update=updates),
            expected_capability_id=outcome.capability_id,
            expected_capability_version=outcome.capability_version,
            expected_team_version=outcome.team_version,
        )


def test_terminal_decision_uses_only_the_closed_factory_state_vocabulary() -> None:
    decision = FactoryTerminalDecision(
        schema_name="captain.factory-terminal-decision.v1",
        decision_id="00000000-0000-0000-0000-000000000051",
        job_id="00000000-0000-0000-0000-000000000011",
        correlation_id="00000000-0000-0000-0000-000000000012",
        subject_version=1,
        state=FactoryTerminalState.BLOCKED,
        reasons=("missing required CRM credential alias",),
        evidence_refs=(),
        decided_at="2026-07-21T08:30:00Z",
    )

    assert decision.state is FactoryTerminalState.BLOCKED
    with pytest.raises(ValidationError):
        FactoryTerminalDecision.model_validate(
            {**decision.model_dump(mode="json", by_alias=True), "state": "succeeded"}
        )
