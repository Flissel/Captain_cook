from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError
from pydantic import SecretStr

from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from gateway.portal_live_contracts import (
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


def test_oauth_completion_requires_consent_and_callback_evidence() -> None:
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
        "status": "passed",
        "occurred_at": NOW,
    }

    with pytest.raises(ValidationError, match="consent and callback"):
        PortalProviderProbeCompletionV1(**common)

    completion = PortalProviderProbeCompletionV1(
        **common,
        consent_ref=_ref("oauth-consent", "e" * 64),
        callback_ref=_ref("oauth-callback", "f" * 64),
    )
    assert completion.status == "passed"


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
