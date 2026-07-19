from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from agenten.agent_runtime.capabilities import (
    CapabilityDenied,
    derive_grant,
)
from agenten.agent_runtime.contracts import AgentRuntimeCommand
from agenten.agent_runtime.contracts import CapabilityGrantRevocation
from agenten.agent_runtime.n8n_endpoint import (
    N8nEndpoint,
    N8nEndpointConfigurationError,
    build_hermes_n8n_reference,
    resolve_n8n_endpoint,
)
from agenten.validation.contracts import AcceptanceAssertion, AssertionKind, WorkBatch


NOW = datetime(2026, 7, 18, 12, tzinfo=timezone.utc)
COMMAND_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures"
    / "contracts"
    / "agent_runtime_command.v1.json"
)


def command_for(
    *,
    profile: str = "n8n-builder",
    intent: str = "n8n",
) -> AgentRuntimeCommand:
    value: dict[str, Any] = json.loads(COMMAND_FIXTURE.read_text(encoding="utf-8"))
    value["event_id"] = str(uuid4())
    value["occurred_at"] = NOW.isoformat().replace("+00:00", "Z")
    payload = value["payload"]
    assert isinstance(payload, dict)
    payload.update(
        {
            "capability_profile": profile,
            "integration_intent": intent,
        }
    )
    return AgentRuntimeCommand.model_validate(value)


def released_batch(*, capability_tag: str) -> WorkBatch:
    return WorkBatch(
        batch_id="batch-1",
        title="Build the approved integration",
        goal="Implement the released subtask in its authorized workspace.",
        subtask_ids=["subtask-1"],
        target="n8n",
        capability_tags=[capability_tag],
        acceptance_criteria=[
            AcceptanceAssertion(
                assertion_id="focused-tests-pass",
                kind=AssertionKind.STATUS_EQUALS,
                path="status",
                expected="passed",
            )
        ],
    )


def builder_endpoint() -> N8nEndpoint:
    return resolve_n8n_endpoint(
        {
            "N8N_MODE": "captain-builder",
            "CAPTAIN_N8N_URL": "http://localhost:5679",
            "CAPTAIN_N8N_API_KEY": "sensitive-key-for-redaction",
            "CAPTAIN_N8N_MCP_TOKEN": "sensitive-mcp-token-for-redaction",
        }
    )


def test_builder_mode_uses_only_captain_values() -> None:
    endpoint = resolve_n8n_endpoint(
        {
            "N8N_MODE": "captain-builder",
            "CAPTAIN_N8N_URL": "http://localhost:5679",
            "CAPTAIN_N8N_API_KEY": "sensitive-key-for-redaction",
            "CAPTAIN_N8N_MCP_TOKEN": "sensitive-mcp-token-for-redaction",
            "N8N_URL": "http://localhost:15678",
            "N8N_MCP_TOKEN": "unselected-external-key",
        }
    )

    assert endpoint.api_base_url == "http://localhost:5679"
    assert endpoint.webhook_base_url == "http://localhost:5679"
    assert endpoint.api_key == "sensitive-key-for-redaction"
    assert endpoint.mcp_token == "sensitive-mcp-token-for-redaction"
    assert "sensitive-key-for-redaction" not in repr(endpoint)
    assert "sensitive-key-for-redaction" not in endpoint.model_dump_json()
    assert "sensitive-mcp-token-for-redaction" not in endpoint.model_dump_json()
    assert endpoint.model_dump() == {
        "mode": "captain-builder",
        "api_base_url": "http://localhost:5679",
        "webhook_base_url": "http://localhost:5679",
    }


def test_builder_mode_rejects_vibemind_url() -> None:
    with pytest.raises(N8nEndpointConfigurationError):
        resolve_n8n_endpoint(
            {
                "N8N_MODE": "captain-builder",
                "CAPTAIN_N8N_URL": "http://localhost:15678",
                "CAPTAIN_N8N_API_KEY": "sensitive-key-for-redaction",
            }
        )


@pytest.mark.parametrize("mode", ["builder", "Captain-builder", "", "external "])
def test_endpoint_rejects_unknown_or_inexact_modes(mode: str) -> None:
    with pytest.raises(N8nEndpointConfigurationError, match="N8N_MODE"):
        resolve_n8n_endpoint({"N8N_MODE": mode})


def test_endpoint_requires_explicit_mode() -> None:
    with pytest.raises(N8nEndpointConfigurationError, match="N8N_MODE"):
        resolve_n8n_endpoint(
            {
                "N8N_URL": "http://localhost:15678",
                "N8N_MCP_TOKEN": "sensitive-key-for-redaction",
            }
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:5679",
        "http://example.com:5679",
        "http://captain:password@localhost:5679",
        "http://localhost",
        "not-a-url",
    ],
)
def test_builder_mode_requires_explicit_loopback_http_url_without_userinfo(
    url: str,
) -> None:
    with pytest.raises(N8nEndpointConfigurationError, match="loopback HTTP"):
        resolve_n8n_endpoint(
            {
                "N8N_MODE": "captain-builder",
                "CAPTAIN_N8N_URL": url,
                "CAPTAIN_N8N_API_KEY": "sensitive-key-for-redaction",
            }
        )


def test_builder_mode_requires_nonempty_captain_api_key() -> None:
    with pytest.raises(N8nEndpointConfigurationError, match="API key"):
        resolve_n8n_endpoint(
            {
                "N8N_MODE": "captain-builder",
                "CAPTAIN_N8N_URL": "http://127.0.0.1:5679",
                "CAPTAIN_N8N_API_KEY": "   ",
            }
        )


def test_external_mode_retains_existing_environment_contract() -> None:
    endpoint = resolve_n8n_endpoint(
        {
            "N8N_MODE": "external",
            "N8N_URL": "http://localhost:15678/",
            "N8N_MCP_TOKEN": "sensitive-key-for-redaction",
            "CAPTAIN_N8N_URL": "http://localhost:5679",
            "CAPTAIN_N8N_API_KEY": "unselected-captain-key",
        }
    )

    assert endpoint.mode == "external"
    assert endpoint.api_base_url == "http://localhost:15678"
    assert endpoint.webhook_base_url == "http://localhost:15678"
    assert endpoint.api_key == "sensitive-key-for-redaction"


def test_external_mode_rejects_configured_captain_endpoint_identity() -> None:
    with pytest.raises(N8nEndpointConfigurationError, match="CAPTAIN_N8N_URL"):
        resolve_n8n_endpoint(
            {
                "N8N_MODE": "external",
                "N8N_URL": "http://LOCALHOST:5679/",
                "N8N_MCP_TOKEN": "sensitive-key-for-redaction",
                "CAPTAIN_N8N_URL": "http://localhost:5679",
            }
        )


@pytest.mark.parametrize(
    "external_url",
    ["http://127.0.0.1:5679", "http://[::1]:5679"],
)
def test_external_mode_rejects_configured_captain_loopback_alias(
    external_url: str,
) -> None:
    with pytest.raises(N8nEndpointConfigurationError, match="CAPTAIN_N8N_URL"):
        resolve_n8n_endpoint(
            {
                "N8N_MODE": "external",
                "N8N_URL": external_url,
                "N8N_MCP_TOKEN": "sensitive-key-for-redaction",
                "CAPTAIN_N8N_URL": "http://localhost:5679",
            }
        )


@pytest.mark.parametrize(
    "external_url",
    ["http://localhost.:5679", "http://localhost:5679/."],
)
def test_external_mode_rejects_equivalent_captain_uri_variant(
    external_url: str,
) -> None:
    with pytest.raises(N8nEndpointConfigurationError):
        resolve_n8n_endpoint(
            {
                "N8N_MODE": "external",
                "N8N_URL": external_url,
                "N8N_MCP_TOKEN": "sensitive-key-for-redaction",
                "CAPTAIN_N8N_URL": "http://localhost:5679",
            }
        )


def test_external_mode_allows_unrelated_url_when_captain_url_is_configured() -> None:
    endpoint = resolve_n8n_endpoint(
        {
            "N8N_MODE": "external",
            "N8N_URL": "https://automation.example.test/n8n",
            "N8N_MCP_TOKEN": "sensitive-key-for-redaction",
            "CAPTAIN_N8N_URL": "http://localhost:5679",
        }
    )

    assert endpoint.api_base_url == "https://automation.example.test/n8n"


@pytest.mark.parametrize("mode", ["external", "captain-builder"])
def test_endpoint_rejects_query_or_fragment_that_could_leak_secrets(
    mode: str,
) -> None:
    if mode == "external":
        environment = {
            "N8N_MODE": mode,
            "N8N_URL": "http://localhost:15678?credential=unsafe",
            "N8N_MCP_TOKEN": "sensitive-key-for-redaction",
        }
    else:
        environment = {
            "N8N_MODE": mode,
            "CAPTAIN_N8N_URL": "http://localhost:5679#credential-unsafe",
            "CAPTAIN_N8N_API_KEY": "sensitive-key-for-redaction",
        }

    with pytest.raises(N8nEndpointConfigurationError, match="query or fragment"):
        resolve_n8n_endpoint(environment)


@pytest.mark.parametrize(
    ("environment", "missing_name"),
    [
        ({"N8N_MODE": "external", "N8N_MCP_TOKEN": "key"}, "N8N_URL"),
        (
            {"N8N_MODE": "external", "N8N_URL": "http://localhost:15678"},
            "N8N_MCP_TOKEN",
        ),
    ],
)
def test_external_mode_fails_closed_when_selected_values_are_missing(
    environment: dict[str, str],
    missing_name: str,
) -> None:
    with pytest.raises(N8nEndpointConfigurationError, match=missing_name):
        resolve_n8n_endpoint(environment)


def test_endpoint_is_immutable() -> None:
    endpoint = resolve_n8n_endpoint(
        {
            "N8N_MODE": "captain-builder",
            "CAPTAIN_N8N_URL": "http://[::1]:5679",
            "CAPTAIN_N8N_API_KEY": "sensitive-key-for-redaction",
        }
    )

    with pytest.raises(ValidationError):
        endpoint.api_base_url = "http://localhost:15678"


def test_hermes_reference_is_lease_bound_and_secret_excluded() -> None:
    command = command_for()
    grant = derive_grant(
        command,
        released_batch(capability_tag="n8n-builder"),
        NOW,
    )

    reference = build_hermes_n8n_reference(
        grant,
        command,
        builder_endpoint(),
        NOW + timedelta(seconds=1),
    )

    assert reference.model_dump() == {
        "endpoint_identity": "http://localhost:5679",
        "server_name": "n8n-mcp",
    }
    assert reference.child_process_environment() == {
        "N8N_URL": "http://localhost:5679",
        "N8N_MCP_TOKEN": "sensitive-mcp-token-for-redaction",
    }
    assert "sensitive-key-for-redaction" not in repr(reference)
    assert "sensitive-mcp-token-for-redaction" not in repr(reference)
    assert "sensitive-key-for-redaction" not in reference.model_dump_json()
    assert "sensitive-mcp-token-for-redaction" not in reference.model_dump_json()


def test_hermes_reference_returns_a_fresh_child_environment() -> None:
    command = command_for()
    grant = derive_grant(
        command,
        released_batch(capability_tag="n8n-builder"),
        NOW,
    )
    reference = build_hermes_n8n_reference(
        grant,
        command,
        builder_endpoint(),
        NOW + timedelta(seconds=1),
    )

    child_environment = reference.child_process_environment()
    child_environment["N8N_MCP_TOKEN"] = "changed-by-child"

    assert reference.child_process_environment()["N8N_MCP_TOKEN"] == (
        "sensitive-mcp-token-for-redaction"
    )


def test_hermes_reference_rejects_external_endpoint() -> None:
    command = command_for()
    grant = derive_grant(
        command,
        released_batch(capability_tag="n8n-builder"),
        NOW,
    )
    external_endpoint = resolve_n8n_endpoint(
        {
            "N8N_MODE": "external",
            "N8N_URL": "https://n8n.example.test",
            "N8N_MCP_TOKEN": "sensitive-key-for-redaction",
        }
    )

    with pytest.raises(N8nEndpointConfigurationError, match="captain-builder"):
        build_hermes_n8n_reference(
            grant,
            command,
            external_endpoint,
            NOW + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    ("api_base_url", "webhook_base_url"),
    [
        ("http://localhost:15678", "http://localhost:15678"),
        ("https://n8n.example.test", "https://n8n.example.test"),
        ("http://localhost:5679", "http://example.test:5679"),
    ],
)
def test_hermes_reference_rejects_forged_builder_endpoint(
    api_base_url: str,
    webhook_base_url: str,
) -> None:
    command = command_for()
    grant = derive_grant(
        command,
        released_batch(capability_tag="n8n-builder"),
        NOW,
    )
    forged_endpoint = N8nEndpoint(
        mode="captain-builder",
        api_base_url=api_base_url,
        webhook_base_url=webhook_base_url,
        api_key="sensitive-key-for-redaction",
    )

    with pytest.raises(N8nEndpointConfigurationError):
        build_hermes_n8n_reference(
            grant,
            command,
            forged_endpoint,
            NOW + timedelta(seconds=1),
        )


def test_expired_grant_cannot_create_hermes_reference() -> None:
    command = command_for()
    grant = derive_grant(
        command,
        released_batch(capability_tag="n8n-builder"),
        NOW - timedelta(minutes=16),
    )

    with pytest.raises(CapabilityDenied, match="expired"):
        build_hermes_n8n_reference(grant, command, builder_endpoint(), NOW)


def test_revoked_grant_cannot_create_hermes_reference() -> None:
    command = command_for()
    grant = derive_grant(
        command,
        released_batch(capability_tag="n8n-builder"),
        NOW,
    )
    revocation = CapabilityGrantRevocation(
        schema_name="captain.capability-grant-revocation.v1",
        revocation_id=uuid4(),
        grant_id=grant.grant_id,
        command_id=command.event_id,
        revoked_at=NOW + timedelta(seconds=1),
        reason="captain_cancelled",
    )

    with pytest.raises(CapabilityDenied, match="revoked"):
        build_hermes_n8n_reference(
            grant,
            command,
            builder_endpoint(),
            NOW + timedelta(seconds=2),
            revocation,
        )


def test_plain_builder_grant_cannot_create_hermes_reference() -> None:
    command = command_for(profile="code-builder", intent="none")
    grant = derive_grant(
        command,
        released_batch(capability_tag="code-builder"),
        NOW,
    )

    with pytest.raises(CapabilityDenied, match="n8n-builder"):
        build_hermes_n8n_reference(
            grant,
            command,
            builder_endpoint(),
            NOW + timedelta(seconds=1),
        )


def test_wrong_server_grant_cannot_create_hermes_reference() -> None:
    command = command_for()
    grant = derive_grant(
        command,
        released_batch(capability_tag="n8n-builder"),
        NOW,
    ).model_copy(update={"mcp_servers": ("other-server",)})

    with pytest.raises(CapabilityDenied, match="MCP servers"):
        build_hermes_n8n_reference(
            grant,
            command,
            builder_endpoint(),
            NOW + timedelta(seconds=1),
        )


def test_grant_for_another_command_cannot_create_hermes_reference() -> None:
    command = command_for()
    grant = derive_grant(
        command,
        released_batch(capability_tag="n8n-builder"),
        NOW,
    )

    with pytest.raises(CapabilityDenied, match="command"):
        build_hermes_n8n_reference(
            grant,
            command_for(),
            builder_endpoint(),
            NOW + timedelta(seconds=1),
        )


def test_example_environment_keeps_external_default_and_documents_builder_opt_in() -> None:
    example = Path(".env.example").read_text(encoding="utf-8")

    assert "N8N_MODE=external" in example
    assert "CAPTAIN_N8N_URL=http://localhost:5679" in example
    assert "CAPTAIN_N8N_API_KEY=" in example
    assert "CAPTAIN_N8N_MCP_TOKEN=" in example
    assert "CAPTAIN_N8N_PORT=5679" in example
