from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from agenten.agent_runtime.capabilities import (
    PROFILE_CAPABILITIES,
    CapabilityDenied,
    derive_grant,
    validate_grant,
)
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    CapabilityGrant,
    CapabilityProfile,
)
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, WorkBatch


NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "agent_runtime_command.v1.json"
)


def command_for(
    *,
    profile: str = "n8n-builder",
    intent: str = "n8n",
    operation: str = "codex.run",
    workspace_ref: str = "workspace://authorized/project-1/subtask-1",
) -> AgentRuntimeCommand:
    value: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    value["event_id"] = str(uuid4())
    value["occurred_at"] = NOW.isoformat().replace("+00:00", "Z")
    payload = value["payload"]
    assert isinstance(payload, dict)
    payload.update(
        {
            "operation": operation,
            "capability_profile": profile,
            "integration_intent": intent,
            "workspace_ref": workspace_ref,
        }
    )
    return AgentRuntimeCommand.model_validate(value)


def released_batch(*, capability_tags: list[str]) -> WorkBatch:
    return WorkBatch(
        batch_id="batch-1",
        title="Build the approved integration",
        goal="Implement the released subtask in its authorized workspace.",
        subtask_ids=["subtask-1"],
        target="python",
        capability_tags=capability_tags,
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="focused-tests-pass",
                kind=AssertionKind.STATUS_EQUALS,
                path="status",
                expected="passed",
            )
        ],
    )


def test_swarm_cannot_self_grant_n8n() -> None:
    command = command_for()
    batch = released_batch(capability_tags=["code-builder"])

    with pytest.raises(CapabilityDenied, match="n8n-builder was not released"):
        derive_grant(command, batch, NOW)


def test_derived_n8n_grant_is_exact_short_lived_and_token_free() -> None:
    command = command_for()
    batch = released_batch(capability_tags=["n8n-builder"])

    grant = derive_grant(command, batch, NOW)

    assert grant.command_id == command.event_id
    assert grant.batch_id == batch.batch_id
    assert grant.batch_version == command.subject_version
    assert grant.subtask_id == command.payload.subtask_id
    assert grant.workspace_ref == command.payload.workspace_ref
    assert grant.profile is CapabilityProfile.N8N_BUILDER
    assert frozenset(grant.capabilities) == PROFILE_CAPABILITIES[grant.profile]
    assert grant.mcp_servers == ("n8n-mcp",)
    assert grant.expires_at == NOW + timedelta(minutes=15)
    assert "token" not in grant.model_dump_json().lower()


def test_expired_grant_cannot_resume_codex() -> None:
    command = command_for(operation="codex.resume")
    grant = derive_grant(
        command,
        released_batch(capability_tags=["n8n-builder"]),
        NOW - timedelta(minutes=16),
    )

    with pytest.raises(CapabilityDenied, match="expired"):
        validate_grant(grant, command, NOW)


def test_grant_replay_for_another_command_is_denied() -> None:
    original = command_for()
    replay = command_for()
    grant = derive_grant(
        original,
        released_batch(capability_tags=["n8n-builder"]),
        NOW,
    )

    with pytest.raises(CapabilityDenied, match="command"):
        validate_grant(grant, replay, NOW)


def test_workspace_mismatch_is_denied() -> None:
    command = command_for(profile="code-builder", intent="none")
    grant = derive_grant(
        command,
        released_batch(capability_tags=["code-builder"]),
        NOW,
    )
    changed = command_for(
        profile="code-builder",
        intent="none",
        workspace_ref="workspace://authorized/project-1/other-subtask",
    )
    changed_value = changed.model_dump(mode="json", by_alias=True)
    changed_value["event_id"] = str(command.event_id)
    changed_value["subject_id"] = command.subject_id
    changed_value["payload"]["subtask_id"] = command.payload.subtask_id
    changed = AgentRuntimeCommand.model_validate(changed_value)

    with pytest.raises(CapabilityDenied, match="workspace"):
        validate_grant(grant, changed, NOW)


def test_profile_escalation_is_denied_even_with_matching_command_id() -> None:
    plain = command_for(profile="code-builder", intent="none")
    plain_grant = derive_grant(
        plain,
        released_batch(capability_tags=["code-builder"]),
        NOW,
    )
    escalated_value = plain.model_dump(mode="json", by_alias=True)
    escalated_value["payload"]["capability_profile"] = "n8n-builder"
    escalated_value["payload"]["integration_intent"] = "n8n"
    escalated = AgentRuntimeCommand.model_validate(escalated_value)

    with pytest.raises(CapabilityDenied, match="profile"):
        validate_grant(plain_grant, escalated, NOW)


def test_batch_binding_checks_subtask_and_released_profile() -> None:
    command = command_for(profile="code-builder", intent="none")
    wrong_subtask = released_batch(capability_tags=["code-builder"]).model_copy(
        update={"subtask_ids": ["subtask-2"]}
    )

    with pytest.raises(CapabilityDenied, match="subtask"):
        derive_grant(command, wrong_subtask, NOW)


def test_valid_grant_round_trip_is_accepted() -> None:
    command = command_for(profile="code-builder", intent="none")
    grant = derive_grant(
        command,
        released_batch(capability_tags=["code-builder"]),
        NOW,
    )
    reloaded = CapabilityGrant.model_validate_json(
        grant.model_dump_json(by_alias=True)
    )

    assert validate_grant(reloaded, command, NOW + timedelta(seconds=1)) == reloaded
