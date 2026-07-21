from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
import json
from pathlib import Path

from agenten.agent_runtime.contracts import CapabilityProfile

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    AgentFactoryJobV2,
    AgentFactoryJobV3,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
    FactoryLease,
    PromotedCapability,
    parse_factory_job,
)


NOW = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)


def artifact(name: str) -> dict[str, str]:
    return {
        "uri": f"artifact://factory/{name}",
        "sha256": "a" * 64,
        "media_type": "application/json",
    }


def job_payload() -> dict[str, object]:
    return {
        "schema": "captain.agent-factory-job.v1",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "correlation_id": "00000000-0000-0000-0000-000000000002",
        "causation_id": None,
        "occurred_at": NOW,
        "producer": "captain",
        "job_id": "00000000-0000-0000-0000-000000000003",
        "subject_version": 1,
        "input_ref": artifact("input"),
        "required_capability": "support_triage",
        "acceptance_assertion_ids": ["schema_valid", "real_case_green"],
        "max_behavioral_iterations": 5,
    }


def block_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": "captain.agent-factory-block.v1",
        "event_id": "00000000-0000-0000-0000-000000000004",
        "job_id": "00000000-0000-0000-0000-000000000003",
        "correlation_id": "00000000-0000-0000-0000-000000000002",
        "causation_id": "00000000-0000-0000-0000-000000000001",
        "occurred_at": NOW,
        "producer": "hermes",
        "subject_version": 1,
        "attempt": 1,
        "phase": "blueprint_created",
        "role": "agent_architect",
        "status": "succeeded",
        "artifact_refs": [artifact("blueprint")],
        "evidence_refs": [artifact("evidence")],
        "assertion_ids": ["schema_valid"],
        "lease_id": "lease-1",
    }
    payload.update(overrides)
    return payload


def test_factory_job_is_strict_and_fixed_to_five_iterations() -> None:
    job = AgentFactoryJob.model_validate(job_payload())

    assert job.schema_name == "captain.agent-factory-job.v1"
    assert job.max_behavioral_iterations == 5
    assert job.event_id == UUID(int=1)

    with pytest.raises(ValidationError):
        AgentFactoryJob.model_validate({**job_payload(), "unexpected": True})


def test_factory_job_rejects_duplicate_assertions_and_non_utc_time() -> None:
    with pytest.raises(ValidationError, match="duplicates"):
        AgentFactoryJob.model_validate(
            {**job_payload(), "acceptance_assertion_ids": ["schema_valid", "schema_valid"]}
        )


def test_factory_job_v1_remains_readable_and_v2_fixture_is_strict() -> None:
    assert isinstance(parse_factory_job(job_payload()), AgentFactoryJob)
    fixture = Path(__file__).parents[1] / "fixtures" / "agent_factory" / "agent_factory_job.v2.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    job = parse_factory_job(payload)
    assert isinstance(job, AgentFactoryJobV2)
    assert job.schema_name == "captain.agent-factory-job.v2"
    assert job.max_behavioral_iterations == 5
    assert job.deadline_at > job.occurred_at

    with pytest.raises(ValidationError, match="deadline"):
        AgentFactoryJobV2.model_validate({**payload, "deadline_at": payload["occurred_at"]})
    duplicate_assertions = {**payload, "acceptance_assertion_ids": [payload["acceptance_assertion_ids"][0]] * 2}
    with pytest.raises(ValidationError, match="duplicates"):
        AgentFactoryJobV2.model_validate(duplicate_assertions)
    duplicate_holdouts = {**payload, "private_holdout_refs": [payload["private_holdout_refs"][0]] * 2}
    with pytest.raises(ValidationError, match="duplicates"):
        AgentFactoryJobV2.model_validate(duplicate_holdouts)
    mismatch = dict(payload)
    mismatch["compiled_spec_ref"] = {**payload["compiled_spec_ref"], "sha256": "f" * 64}
    with pytest.raises(ValidationError, match="digest"):
        AgentFactoryJobV2.model_validate(mismatch)

    with pytest.raises(ValidationError, match="UTC"):
        AgentFactoryJob.model_validate(
            {**job_payload(), "occurred_at": datetime(2026, 7, 19, 10)}
        )


def test_factory_job_v3_is_additive_strict_and_preserves_older_parsing() -> None:
    fixture = Path(__file__).parents[1] / "fixtures" / "agent_factory" / "agent_factory_job.v2.json"
    v2_payload = json.loads(fixture.read_text(encoding="utf-8"))
    v3_payload = v2_payload | {
        "schema": "captain.agent-factory-job.v3",
        "execution_policy": {
            "schema": "captain.factory-execution-policy.v1",
            "mode": "release",
            "live_execution": True,
            "max_cost_usd": "5.00",
            "max_runtime_seconds": 900,
            "required_live_runs": 3,
            "allowed_models": ["approved-model-id"],
            "live_capabilities": ["model.invoke", "docker.run"],
            "sandbox_mode": "workspace_write",
        },
    }

    assert isinstance(parse_factory_job(job_payload()), AgentFactoryJob)
    assert isinstance(parse_factory_job(v2_payload), AgentFactoryJobV2)
    parsed = parse_factory_job(v3_payload)
    assert isinstance(parsed, AgentFactoryJobV3)
    assert parsed.max_behavioral_iterations == 5

    with pytest.raises(ValidationError):
        AgentFactoryJobV3.model_validate(v3_payload | {"unexpected": True})
    with pytest.raises((TypeError, ValidationError)):
        AgentFactoryJobV3.model_validate(
            v3_payload
            | {
                "execution_policy": v3_payload["execution_policy"]
                | {"max_cost_usd": 5.0}
            }
        )
    with pytest.raises(ValidationError, match="max_runtime_seconds"):
        AgentFactoryJobV3.model_validate(
            v3_payload | {"deadline_at": "2026-07-21T12:14:59Z"}
        )


def test_evidence_block_binds_phase_role_and_lease() -> None:
    block = FactoryEvidenceBlock.model_validate(block_payload())

    assert block.phase is FactoryPhase.BLUEPRINT_CREATED
    assert block.role is FactoryRole.AGENT_ARCHITECT
    assert block.status is FactoryBlockStatus.SUCCEEDED

    with pytest.raises(ValidationError, match="ToolIntegrator"):
        FactoryEvidenceBlock.model_validate(
            block_payload(phase="tool_candidate_tested", role="agent_architect")
        )

    with pytest.raises(ValidationError, match="lease"):
        FactoryEvidenceBlock.model_validate(block_payload(lease_id=None))


def test_hermes_role_block_requires_verifiable_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        FactoryEvidenceBlock.model_validate(block_payload(evidence_refs=[]))


def test_promotion_is_captain_only_and_requires_complete_evidence() -> None:
    with pytest.raises(ValidationError, match="Captain"):
        FactoryEvidenceBlock.model_validate(
            block_payload(
                phase="capability_promoted",
                role=None,
                producer="hermes",
                lease_id=None,
                assertion_ids=["schema_valid", "real_case_green"],
            )
        )

    with pytest.raises(ValidationError, match="assertion"):
        FactoryEvidenceBlock.model_validate(
            block_payload(
                phase="capability_promoted",
                role=None,
                producer="captain",
                lease_id=None,
                assertion_ids=[],
            )
        )


def test_promoted_capability_requires_captain_promotion_reference() -> None:
    with pytest.raises(ValidationError, match="promotion"):
        PromotedCapability.model_validate(
            {
                "capability_id": "support_triage",
                "version": 1,
                "status": "ready_to_use",
                "blueprint_ref": artifact("blueprint"),
                "code_ref": artifact("code"),
                "tool_refs": [],
                "promotion_block_ref": None,
            }
        )


def test_factory_lease_is_short_lived_and_role_scoped() -> None:
    lease = FactoryLease.model_validate(
        {
            "schema": "captain.factory-lease.v1",
            "lease_id": "factory-lease-1",
            "job_id": "00000000-0000-0000-0000-000000000003",
            "correlation_id": "00000000-0000-0000-0000-000000000002",
            "subject_version": 1,
            "attempt": 1,
            "role": "agent_architect",
            "capability_profile": "factory-architect",
            "capabilities": ["hermes.plan", "context7.query"],
            "workspace_ref": "workspace://factory/support-triage",
            "issued_at": NOW,
            "expires_at": datetime(2026, 7, 19, 10, 15, tzinfo=timezone.utc),
        }
    )

    assert lease.capability_profile is CapabilityProfile.FACTORY_ARCHITECT

    with pytest.raises(ValidationError, match="does not match"):
        FactoryLease.model_validate(
            lease.model_dump(mode="json", by_alias=True)
            | {"capability_profile": "factory-tool-integrator"}
        )
