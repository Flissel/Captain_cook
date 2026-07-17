from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import pytest
import yaml
from pydantic import ValidationError

from agenten.agent_runtime.contracts import (
    AgentBlueprint,
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    HermesPlanResult,
    RuntimeStatus,
    canonical_json_bytes,
)


FIXTURES = Path(__file__).parents[1] / "fixtures" / "contracts"
NOW = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)


def artifact(name: str) -> dict[str, str]:
    return {
        "uri": f"artifact://runtime/{name}",
        "sha256": "a" * 64,
        "media_type": "text/markdown",
    }


def command_data() -> dict[str, object]:
    return {
        "schema": "captain.agent-runtime-command.v1",
        "event_id": "00000000-0000-0000-0000-000000000001",
        "correlation_id": "00000000-0000-0000-0000-000000000002",
        "causation_id": None,
        "occurred_at": NOW.isoformat().replace("+00:00", "Z"),
        "producer": "captain-swarm",
        "subject_id": "subtask-1",
        "subject_version": 3,
        "payload": {
            "operation": "codex.run",
            "project_id": "project-1",
            "batch_id": "batch-1",
            "subtask_id": "subtask-1",
            "workspace_ref": "workspace://authorized/project-1/subtask-1",
            "prompt_ref": artifact("prompt-1"),
            "integration_intent": "n8n",
            "capability_profile": "n8n-builder",
            "limits": {"wall_seconds": 900, "max_iterations": 3},
        },
    }


def blueprint_data() -> dict[str, object]:
    return {
        "schema": "captain.agent-blueprint.v1",
        "name": "integration_researcher",
        "purpose": "Discover supported integrations and return a typed recommendation.",
        "inputs": {"project_context": "object"},
        "outputs": {"recommendation": "object"},
        "system_prompt_ref": artifact("system-prompt"),
        "tools": ["knowledge.search"],
        "integration_intent": "none",
        "n8n_tool_families": [],
        "handoffs": ["captain.decompose"],
        "limits": {"max_turns": 8, "wall_seconds": 300},
        "evaluation_cases": [
            {
                "case_id": "rejects-unapproved-tools",
                "assertion": "tool_allowlist_enforced",
            }
        ],
    }


def test_n8n_profile_requires_approved_intent() -> None:
    value = command_data()
    value["payload"] = {  # type: ignore[index]
        **value["payload"],  # type: ignore[misc]
        "integration_intent": "none",
    }

    with pytest.raises(ValidationError, match="n8n-builder requires integration_intent=n8n"):
        AgentRuntimeCommand.model_validate(value)


def test_n8n_intent_cannot_hide_in_plain_builder_profile() -> None:
    value = command_data()
    value["payload"] = {  # type: ignore[index]
        **value["payload"],  # type: ignore[misc]
        "capability_profile": "code-builder",
    }

    with pytest.raises(ValidationError, match="integration_intent=n8n requires n8n-builder"):
        AgentRuntimeCommand.model_validate(value)


def test_codex_command_requires_batch_workspace_and_subtask() -> None:
    value = command_data()
    value["payload"] = {  # type: ignore[index]
        **value["payload"],  # type: ignore[misc]
        "workspace_ref": None,
    }

    with pytest.raises(ValidationError, match="Codex operations require"):
        AgentRuntimeCommand.model_validate(value)


def test_agent_blueprint_rejects_embedded_credentials() -> None:
    value = blueprint_data()
    value["inputs"] = {
        "project_context": "object",
        "credentials": {"N8N_MCP_TOKEN": "secret"},
    }

    with pytest.raises(ValidationError, match="secret-bearing field"):
        AgentBlueprint.model_validate(value)


def test_agent_blueprint_requires_n8n_tool_family_for_n8n_intent() -> None:
    value = blueprint_data()
    value["integration_intent"] = "n8n"

    with pytest.raises(ValidationError, match="n8n intent requires tool families"):
        AgentBlueprint.model_validate(value)


def test_runtime_result_requires_failure_details_and_echoes_command() -> None:
    with pytest.raises(ValidationError, match="failed runtime results require an error"):
        AgentRuntimeResult.model_validate(
            {
                "schema": "captain.agent-runtime-result.v1",
                "event_id": "00000000-0000-0000-0000-000000000003",
                "command_id": "00000000-0000-0000-0000-000000000001",
                "correlation_id": "00000000-0000-0000-0000-000000000002",
                "occurred_at": NOW,
                "producer": "agent-runtime",
                "subject_id": "subtask-1",
                "subject_version": 3,
                "grant_id": "grant-1",
                "operation": "codex.run",
                "status": "failed",
                "session_id": "session-1",
                "artifact_refs": [],
                "evidence_refs": [],
                "error": None,
            }
        )


def test_capability_grant_rejects_secrets_and_invalid_lifetime() -> None:
    with pytest.raises(ValidationError, match="expires_at must be later"):
        CapabilityGrant.model_validate(
            {
                "schema": "captain.capability-grant.v1",
                "grant_id": "grant-1",
                "command_id": "00000000-0000-0000-0000-000000000001",
                "batch_id": "batch-1",
                "batch_version": 3,
                "subtask_id": "subtask-1",
                "workspace_ref": "workspace://authorized/project-1/subtask-1",
                "profile": "n8n-builder",
                "capabilities": ["codex.run", "mcp.n8n"],
                "mcp_servers": ["n8n-mcp"],
                "issued_at": NOW,
                "expires_at": NOW - timedelta(seconds=1),
            }
        )


@pytest.mark.parametrize("bad_uri", ["C:/secret/file", "https://example.com/file", "artifact:/broken"])
def test_artifact_refs_are_opaque_and_content_addressed(bad_uri: str) -> None:
    with pytest.raises(ValidationError, match="artifact://"):
        ArtifactRef(uri=bad_uri, sha256="a" * 64, media_type="text/plain")


def test_checked_in_contract_fixtures_round_trip_canonically() -> None:
    command = AgentRuntimeCommand.model_validate_json(
        (FIXTURES / "agent_runtime_command.v1.json").read_text(encoding="utf-8")
    )
    result = AgentRuntimeResult.model_validate_json(
        (FIXTURES / "agent_runtime_result.v1.json").read_text(encoding="utf-8")
    )
    plan = HermesPlanResult.model_validate_json(
        (FIXTURES / "hermes_plan_result.v1.json").read_text(encoding="utf-8")
    )
    blueprint = AgentBlueprint.model_validate(
        yaml.safe_load((FIXTURES / "agent_blueprint.v1.yaml").read_text(encoding="utf-8"))
    )

    assert canonical_json_bytes(command) == canonical_json_bytes(
        AgentRuntimeCommand.model_validate_json(canonical_json_bytes(command))
    )
    assert canonical_json_bytes(result) == canonical_json_bytes(
        AgentRuntimeResult.model_validate_json(canonical_json_bytes(result))
    )
    assert canonical_json_bytes(plan) == canonical_json_bytes(
        HermesPlanResult.model_validate_json(canonical_json_bytes(plan))
    )
    assert blueprint.name == "integration_researcher"
    assert command.event_id == UUID(int=1)
    assert result.status is RuntimeStatus.SUCCEEDED
    assert result.command_id == command.event_id
    assert plan.plan_ref.sha256 == "b" * 64


def test_fixture_payloads_do_not_contain_secret_or_absolute_user_paths() -> None:
    fixture_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(FIXTURES.glob("agent_*"))
    ) + (FIXTURES / "hermes_plan_result.v1.json").read_text(encoding="utf-8")
    lowered = fixture_text.lower()

    assert "n8n_mcp_token" not in lowered
    assert "api_key" not in lowered
    assert "c:\\users\\" not in lowered
    assert "/home/" not in lowered
    json.dumps(command_data())
