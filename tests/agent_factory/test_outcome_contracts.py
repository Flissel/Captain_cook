from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from agenten.agent_factory.outcome_contracts import (
    CapabilityPackageManifestV1,
    ExecutionOutcomeV1,
    FactoryTerminalDecision,
    FactoryTerminalState,
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
        {"transcript": "full model conversation"},
        {"output_path": "C:\\Users\\User\\workspace\\result.json"},
        {"notes": "read result from C:\\Users\\User\\workspace\\result.json"},
        {"output_path": "/home/runner/work/result.json"},
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
