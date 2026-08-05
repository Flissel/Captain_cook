from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
from pydantic import SecretStr

from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.integration_setup import oauth_exchange_artifact_ref
from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from gateway.portal_live_contracts import (
    PortalRestartReceiptV1,
    PortalLiveRunFinalizationV1,
    PortalProviderProbeCompletionV1,
    PortalProviderProbeRequestV1,
)
from gateway.settings import GatewaySettings


NOW = datetime(2026, 8, 5, 16, tzinfo=timezone.utc)
JOB_ID = UUID("10000000-0000-4000-8000-000000000001")
CORRELATION_ID = UUID("20000000-0000-4000-8000-000000000001")


def _ref(name: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _release(digest: str) -> GiteaTemplateReleaseV1:
    revision = "1" * 40
    return GiteaTemplateReleaseV1(
        repository="captain/templates",
        revision=revision,
        path="verification/oauth.json",
        contents_url=(
            f"https://gitea.example/captain/templates/raw/commit/{revision}/"
            "verification/oauth.json"
        ),
        sha256=digest,
    )


def test_probe_request_is_exactly_idempotent_and_setup_fenced() -> None:
    request = PortalProviderProbeRequestV1(
        probe_request_id=UUID("30000000-0000-4000-8000-000000000001"),
        run_id="portal-live-v1",
        job_id=JOB_ID,
        correlation_id=CORRELATION_ID,
        integration_kind="bearer",
        credential_alias="CRM_API_KEY",
        credential_id="cred-prod",
        setup_revision=7,
        setup_content_sha256="a" * 64,
        verification_template_sha256="b" * 64,
    )

    assert request.setup_revision == 7
    assert request.model_dump(mode="json")["probe_request_id"] == str(
        request.probe_request_id
    )
    with pytest.raises(ValidationError):
        PortalProviderProbeRequestV1.model_validate(
            request.model_dump() | {"bearer_token": "must-never-enter"}
        )


def test_oauth_client_credentials_completion_requires_token_exchange_evidence() -> None:
    common = {
        "probe_request_id": UUID("30000000-0000-4000-8000-000000000002"),
        "trace_id": UUID("40000000-0000-4000-8000-000000000002"),
        "run_id": "portal-live-v1",
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "integration_kind": "oauth2",
        "credential_alias": "CRM_OAUTH",
        "credential_id": "cred-oauth",
        "setup_revision": 7,
        "setup_content_sha256": "a" * 64,
        "template_ref": _ref("gitea", "b" * 64),
        "template_release": _release("b" * 64),
        "deployed_workflow_ref": _ref("n8n-workflow", "c" * 64),
        "execution_ref": _ref("n8n-execution", "d" * 64),
        "provider_proof_sha256": "1" * 64,
        "provider_probe_id": UUID("50000000-0000-4000-8000-000000000007"),
        "status": "passed",
        "occurred_at": NOW,
    }

    with pytest.raises(ValidationError, match="token exchange"):
        PortalProviderProbeCompletionV1(**common)

    exchange_id = UUID("60000000-0000-4000-8000-000000000007")
    exchange_ref = oauth_exchange_artifact_ref(
        exchange_id=exchange_id,
        provider_trace_id=common["trace_id"],
        provider_proof_sha256=common["provider_proof_sha256"],
    )
    completion = PortalProviderProbeCompletionV1(
        **common,
        oauth_grant_type="client_credentials",
        oauth_exchange_id=exchange_id,
        oauth_exchange_ref=exchange_ref,
    )
    assert completion.status == "passed"

    with pytest.raises(ValidationError, match="cannot contain OAuth"):
        PortalProviderProbeCompletionV1(
            **(common | {"integration_kind": "bearer"}),
            oauth_grant_type="client_credentials",
            oauth_exchange_id=exchange_id,
            oauth_exchange_ref=exchange_ref,
        )


def test_control_plane_tokens_must_be_complete_and_pairwise_distinct() -> None:
    common = {
        "ledger_dsn": SecretStr("mariadb://captain:private@localhost/captain_test"),
        "captain_gateway_token": SecretStr("captain-token"),
        "worker_gateway_token": SecretStr("worker-token"),
        "portal_provider_control_token": SecretStr("provider-token"),
        "portal_evidence_token": SecretStr("evidence-token"),
        "portal_restart_control_token": SecretStr("restart-token"),
    }
    settings = GatewaySettings(**common)
    assert settings.portal_control_configured is True

    with pytest.raises(ValidationError, match="control tokens must be distinct"):
        GatewaySettings(
            **common
            | {
                "portal_evidence_token": SecretStr("provider-token"),
            }
        )
    with pytest.raises(ValidationError, match="configured together"):
        GatewaySettings(
            **{
                key: value
                for key, value in common.items()
                if key != "portal_restart_control_token"
            }
        )


def test_restart_receipt_requires_exact_services_and_new_boot_identity() -> None:
    common = {
        "restart_request_id": UUID("50000000-0000-4000-8000-000000000001"),
        "restart_id": UUID("60000000-0000-4000-8000-000000000001"),
        "run_id": "portal-live-v1",
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "services": ("gateway", "portal"),
        "previous_gateway_boot_id": "boot-before",
        "new_gateway_boot_id": "boot-after",
        "portal_deployment_id": "portal-deployment-1",
        "setup_revision": 7,
        "setup_content_sha256": "a" * 64,
        "status": "resumed",
        "occurred_at": NOW,
    }
    assert PortalRestartReceiptV1(**common).status == "resumed"
    with pytest.raises(ValidationError, match="exactly gateway and portal"):
        PortalRestartReceiptV1(**common | {"services": ("gateway",)})
    with pytest.raises(ValidationError, match="boot identity must change"):
        PortalRestartReceiptV1(
            **common | {"new_gateway_boot_id": "boot-before"}
        )


def test_live_finalization_requires_exactly_three_unique_provider_traces() -> None:
    common = {
        "decision_request_id": UUID("80000000-0000-4000-8000-000000000001"),
        "run_id": "portal-live-v1",
        "job_id": JOB_ID,
        "correlation_id": CORRELATION_ID,
        "provider_trace_ids": (
            UUID("40000000-0000-4000-8000-000000000001"),
            UUID("40000000-0000-4000-8000-000000000002"),
            UUID("40000000-0000-4000-8000-000000000003"),
        ),
        "restart_id": UUID("60000000-0000-4000-8000-000000000001"),
        "minibook_rebuild_id": UUID("70000000-0000-4000-8000-000000000001"),
        "policy_version": "portal-live-v1",
        "occurred_at": NOW,
    }
    assert len(PortalLiveRunFinalizationV1(**common).provider_trace_ids) == 3
    with pytest.raises(ValidationError, match="exactly three"):
        PortalLiveRunFinalizationV1(
            **common | {"provider_trace_ids": common["provider_trace_ids"][:2]}
        )
    with pytest.raises(ValidationError, match="unique"):
        PortalLiveRunFinalizationV1(
            **common
            | {
                "provider_trace_ids": (
                    common["provider_trace_ids"][0],
                    common["provider_trace_ids"][0],
                    common["provider_trace_ids"][2],
                )
            }
        )
