from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
    PromotedCapability,
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

    with pytest.raises(ValidationError, match="UTC"):
        AgentFactoryJob.model_validate(
            {**job_payload(), "occurred_at": datetime(2026, 7, 19, 10)}
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
