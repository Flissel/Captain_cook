from __future__ import annotations

import json
from typing import Iterator

import httpx
import pytest

from tests.live.portal_live_support import (
    MAX_RESPONSE_BYTES,
    PortalLiveClient,
    PortalLiveConfigurationError,
    PortalLiveConfig,
    PortalLiveResponseError,
)


def complete_environment() -> dict[str, str]:
    return {
        "CAPTAIN_PORTAL_LIVE_E2E": "1",
        "CAPTAIN_PORTAL_LIVE_BASE_URL": "https://portal.example.test",
        "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL": "https://captain.example.test",
        "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL": "https://captain.example.test/health",
        "CAPTAIN_PORTAL_LIVE_ORG_A_ACCESS_TOKEN": "org-a-access-value",
        "CAPTAIN_PORTAL_LIVE_ORG_B_ACCESS_TOKEN": "org-b-access-value",
        "CAPTAIN_PORTAL_LIVE_ORG_A_ID": "org-a",
        "CAPTAIN_PORTAL_LIVE_ORG_B_ID": "org-b",
        "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_TOKEN": "captain-control-value",
        "CAPTAIN_PORTAL_LIVE_JOB_ID": "10000000-0000-0000-0000-000000000001",
        "CAPTAIN_PORTAL_LIVE_CORRELATION_ID": "20000000-0000-0000-0000-000000000002",
        "CAPTAIN_PORTAL_LIVE_BEARER_ALIAS": "CRM_BEARER",
        "CAPTAIN_PORTAL_LIVE_BEARER_CREDENTIAL_ID": "credential-bearer",
        "CAPTAIN_PORTAL_LIVE_OAUTH_ALIAS": "CRM_OAUTH",
        "CAPTAIN_PORTAL_LIVE_OAUTH_CREDENTIAL_ID": "credential-oauth",
        "CAPTAIN_PORTAL_LIVE_OAUTH_CLIENT_ID": "sandbox-client",
        "CAPTAIN_PORTAL_LIVE_OAUTH_AUTH_URL": "https://provider.example.test/oauth/authorize",
        "CAPTAIN_PORTAL_LIVE_OAUTH_CALLBACK_URL": "https://n8n.example.test/oauth/callback",
        "CAPTAIN_PORTAL_LIVE_N8N_HEALTH_URL": "https://n8n.example.test/healthz",
        "CAPTAIN_PORTAL_LIVE_GITEA_HEALTH_URL": "https://gitea.example.test/api/healthz",
        "CAPTAIN_PORTAL_LIVE_SUPABASE_HEALTH_URL": "https://supabase.example.test/auth/v1/health",
        "CAPTAIN_PORTAL_LIVE_MINIBOOK_HEALTH_URL": "https://minibook.example.test/health",
        "CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL": "https://provider-control.example.test/v1/provider-audit",
        "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_URL": "https://provider-control.example.test/v1/probes",
        "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL": "https://provider-control.example.test/health",
        "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_TOKEN": "provider-control-value",
        "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL": "https://restart.example.test/v1/restart",
        "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_HEALTH_URL": "https://restart.example.test/health",
        "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_TOKEN": "restart-control-value",
        "CAPTAIN_PORTAL_LIVE_EVIDENCE_URL": "https://evidence.example.test/v1/evidence",
        "CAPTAIN_PORTAL_LIVE_EVIDENCE_HEALTH_URL": "https://evidence.example.test/health",
        "CAPTAIN_PORTAL_LIVE_EVIDENCE_TOKEN": "evidence-read-value",
        "CAPTAIN_PORTAL_LIVE_SECRET_CANARY": "configured-canary-value",
    }


@pytest.fixture(autouse=True)
def clean_live_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in complete_environment():
        monkeypatch.delenv(name, raising=False)
    yield


def install_environment(monkeypatch: pytest.MonkeyPatch, values: dict[str, str]) -> None:
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_opt_in_and_every_control_seam_are_required_before_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(PortalLiveConfigurationError, match="authorize"):
        PortalLiveConfig.from_environment()

    monkeypatch.setenv("CAPTAIN_PORTAL_LIVE_E2E", "1")
    monkeypatch.setenv("CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_TOKEN", "must-not-appear")
    with pytest.raises(PortalLiveConfigurationError) as error:
        PortalLiveConfig.from_environment()
    message = str(error.value)
    assert "CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL" in message
    assert "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL" in message
    assert "CAPTAIN_PORTAL_LIVE_EVIDENCE_URL" in message
    assert "must-not-appear" not in message


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("CAPTAIN_PORTAL_LIVE_BASE_URL", "http://portal.example.test"),
        ("CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL", "https://user:pass@example.test"),
        ("CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL", "https://audit.example.test/x?q=secret"),
        ("CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL", "https://restart.example.test/x#fragment"),
    ),
)
def test_unsafe_urls_fail_before_client_construction(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    environment = complete_environment()
    environment[name] = value
    install_environment(monkeypatch, environment)
    with pytest.raises(PortalLiveConfigurationError, match=name):
        PortalLiveConfig.from_environment()


def test_portal_and_captain_control_origins_must_be_distinct(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = complete_environment()
    environment["CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_BASE_URL"] = environment[
        "CAPTAIN_PORTAL_LIVE_BASE_URL"
    ]
    environment["CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL"] = (
        "https://portal.example.test/health"
    )
    install_environment(monkeypatch, environment)
    with pytest.raises(PortalLiveConfigurationError, match="distinct"):
        PortalLiveConfig.from_environment()


@pytest.mark.parametrize(
    ("name", "value"),
    (
        (
            "CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL",
            "https://other.example.test/health",
        ),
        (
            "CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL",
            "https://other.example.test/v1/audit",
        ),
        (
            "CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL",
            "https://other.example.test/health",
        ),
        (
            "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_HEALTH_URL",
            "https://other.example.test/health",
        ),
        (
            "CAPTAIN_PORTAL_LIVE_EVIDENCE_HEALTH_URL",
            "https://other.example.test/health",
        ),
    ),
)
def test_protected_urls_must_share_their_capability_origin(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    value: str,
) -> None:
    environment = complete_environment()
    environment[name] = value
    install_environment(monkeypatch, environment)
    with pytest.raises(PortalLiveConfigurationError, match="capability origin"):
        PortalLiveConfig.from_environment()


@pytest.mark.parametrize(
    "name",
    (
        "CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL",
        "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL",
        "CAPTAIN_PORTAL_LIVE_EVIDENCE_URL",
    ),
)
def test_protected_origins_cannot_equal_browser_portal_origin(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    environment = complete_environment()
    environment[name] = "https://portal.example.test/protected"
    if name == "CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL":
        environment["CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_URL"] = (
            "https://portal.example.test/provider"
        )
        environment["CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL"] = (
            "https://portal.example.test/health"
        )
    elif name == "CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_URL":
        environment["CAPTAIN_PORTAL_LIVE_RESTART_CONTROL_HEALTH_URL"] = (
            "https://portal.example.test/health"
        )
    else:
        environment["CAPTAIN_PORTAL_LIVE_EVIDENCE_HEALTH_URL"] = (
            "https://portal.example.test/health"
        )
    install_environment(monkeypatch, environment)
    with pytest.raises(PortalLiveConfigurationError, match="browser portal"):
        PortalLiveConfig.from_environment()


def test_malformed_port_is_normalized_to_configuration_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = complete_environment()
    environment["CAPTAIN_PORTAL_LIVE_EVIDENCE_URL"] = (
        "https://evidence.example.test:not-a-port/v1/evidence"
    )
    install_environment(monkeypatch, environment)
    with pytest.raises(PortalLiveConfigurationError, match="EVIDENCE_URL"):
        PortalLiveConfig.from_environment()


def test_explicit_https_default_port_matches_implicit_capability_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = complete_environment()
    environment["CAPTAIN_PORTAL_LIVE_CAPTAIN_CONTROL_HEALTH_URL"] = (
        "https://captain.example.test:443/health"
    )
    install_environment(monkeypatch, environment)
    assert PortalLiveConfig.from_environment().captain_control_health_url.endswith(
        ":443/health"
    )


def test_explicit_default_port_cannot_bypass_browser_origin_separation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = complete_environment()
    environment["CAPTAIN_PORTAL_LIVE_PROVIDER_AUDIT_URL"] = (
        "https://portal.example.test:443/audit"
    )
    environment["CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_URL"] = (
        "https://portal.example.test:443/control"
    )
    environment["CAPTAIN_PORTAL_LIVE_PROVIDER_CONTROL_HEALTH_URL"] = (
        "https://portal.example.test:443/health"
    )
    install_environment(monkeypatch, environment)
    with pytest.raises(PortalLiveConfigurationError, match="browser portal"):
        PortalLiveConfig.from_environment()


def config(monkeypatch: pytest.MonkeyPatch) -> PortalLiveConfig:
    install_environment(monkeypatch, complete_environment())
    return PortalLiveConfig.from_environment()


def test_redirect_is_not_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"Location": "https://other.example.test"})

    client = PortalLiveClient(
        config(monkeypatch),
        portal_transport=httpx.MockTransport(handler),
    )
    try:
        response = client.portal_status("GET", "/v1/portal/integration-setups/missing")
    finally:
        client.close()
    assert response.status_code == 302
    assert calls == 1


def test_oversized_and_malformed_bodies_raise_fixed_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter(
        (
            httpx.Response(200, content=b"x" * (MAX_RESPONSE_BYTES + 1)),
            httpx.Response(200, content=b"not-json"),
            httpx.Response(200, content=b"configured-canary-value malformed"),
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = PortalLiveClient(
        config(monkeypatch),
        portal_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PortalLiveResponseError, match="size limit") as oversized:
            client.get_surface(access_token="access")
        with pytest.raises(PortalLiveResponseError, match="schema") as malformed:
            client.get_surface(access_token="access")
        with pytest.raises(PortalLiveResponseError, match="secret canary") as canary:
            client.get_surface(access_token="access")
    finally:
        client.close()
    assert "configured-canary-value" not in str(oversized.value)
    assert "configured-canary-value" not in str(malformed.value)
    assert "configured-canary-value" not in str(canary.value)


def test_surface_dto_rejects_unknown_and_secret_canary_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = {
        "job_id": "10000000-0000-0000-0000-000000000001",
        "revision": 1,
        "content_sha256": "a" * 64,
        "overall_status": "missing",
        "n8n_credentials_url": "https://n8n.example.test/home/credentials",
        "actions": [],
    }
    responses = iter(
        (
            httpx.Response(200, json={**base, "provider_payload": "unexpected"}),
            httpx.Response(
                200,
                content=json.dumps(
                    {**base, "overall_status": "configured-canary-value"}
                ).encode(),
            ),
        )
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return next(responses)

    client = PortalLiveClient(
        config(monkeypatch),
        portal_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PortalLiveResponseError, match="schema") as unknown:
            client.get_surface(access_token="access")
        with pytest.raises(PortalLiveResponseError, match="secret canary") as canary:
            client.get_surface(access_token="access")
    finally:
        client.close()
    assert "provider_payload" not in str(unknown.value)
    assert "configured-canary-value" not in str(canary.value)


def test_factory_binding_uses_only_the_distinct_captain_control_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def captain_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            201,
            json={
                "job_id": "10000000-0000-0000-0000-000000000001",
                "organization_id": "org-a",
            },
        )

    def portal_handler(_request: httpx.Request) -> httpx.Response:
        pytest.fail("factory binding reached the browser portal origin")

    client = PortalLiveClient(
        config(monkeypatch),
        portal_transport=httpx.MockTransport(portal_handler),
        captain_transport=httpx.MockTransport(captain_handler),
    )
    try:
        receipt = client.provision_tenant("org-a")
    finally:
        client.close()
    assert receipt.organization_id == "org-a"
    assert seen == [
        "https://captain.example.test/v1/factory/integration-setups/"
        "10000000-0000-0000-0000-000000000001/portal-tenant-binding"
    ]


def test_release_evidence_rejects_duplicate_trace_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = {
        "trace_id": "30000000-0000-0000-0000-000000000003",
        "correlation_id": "20000000-0000-0000-0000-000000000002",
        "credential_alias": "CRM_BEARER",
        "credential_id": "credential-bearer",
        "integration_kind": "bearer",
        "status": "passed",
        "occurred_at": "2026-08-05T12:00:00Z",
        "execution_ref": "execution://trace-3",
        "consent_ref": None,
        "callback_ref": None,
    }

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "correlation_id": "20000000-0000-0000-0000-000000000002",
                "revision": 4,
                "status": "accepted",
                "provider_traces": [trace, trace, trace],
                "gitea_sha256": "a" * 64,
                "gateway_decision_ref": "decision://accepted",
                "gateway_execution_ref": "execution://gateway",
                "minibook_projection_ref": "minibook://projection",
                "minibook_rebuild_ref": "minibook://rebuild",
            },
        )

    client = PortalLiveClient(
        config(monkeypatch),
        auxiliary_transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PortalLiveResponseError, match="schema"):
            client.release_evidence()
    finally:
        client.close()
