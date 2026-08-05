"""Real, disposable portal gate; never falls back to mocks."""

from __future__ import annotations

import pytest

from tests.live.portal_live_support import (
    EphemeralPortalTicket,
    PortalActionWire,
    PortalLiveClient,
    PortalLiveConfig,
    PortalLiveConfigurationError,
    PortalLiveResponseError,
    PortalSurfaceEvidence,
)


pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_config() -> PortalLiveConfig:
    """Reject every incomplete/unsafe group before any client or network exists."""

    try:
        return PortalLiveConfig.from_environment()
    except PortalLiveConfigurationError as error:
        pytest.skip(f"missing-live-prerequisites: {error}")


@pytest.fixture(scope="module")
def live_portal(live_config: PortalLiveConfig) -> PortalLiveClient:
    client = PortalLiveClient(live_config)
    try:
        try:
            statuses = client.preflight_statuses()
            audit = client.provider_audit()
        except PortalLiveResponseError:
            pytest.skip("missing-live-prerequisites: a configured live control seam is unavailable")
        if not all(200 <= item.status_code < 300 for item in statuses):
            pytest.skip("missing-live-prerequisites: a configured live control seam is not ready")
        if audit.correlation_id != live_config.correlation_id:
            pytest.skip("missing-live-prerequisites: provider audit is not correlation-bound")
        yield client
    finally:
        client.close()


def _ticket_payload(ticket: EphemeralPortalTicket) -> dict[str, object]:
    return {
        "ticket_id": str(ticket.ticket_id),
        "ticket": ticket.ticket,
        "credential_alias": ticket.credential_alias,
    }


def _find_action(surface: PortalSurfaceEvidence, alias: str) -> PortalActionWire:
    matching = tuple(
        action for action in surface.actions if action.credential_alias == alias
    )
    assert len(matching) == 1
    return matching[0]


def _require_monotonic(revisions: list[int]) -> None:
    assert all(current > previous for previous, current in zip(revisions, revisions[1:]))


def test_live_references_are_reachable_without_forwarding_credentials(
    live_portal: PortalLiveClient,
    live_config: PortalLiveConfig,
) -> None:
    statuses = tuple(
        live_portal.health(url).status_code
        for url in (
            live_config.n8n_health_url,
            live_config.gitea_health_url,
            live_config.supabase_health_url,
            live_config.minibook_health_url,
        )
    )
    assert all(200 <= status < 400 for status in statuses)


def test_portal_rejects_cross_tenant_before_provider_invocation(
    live_portal: PortalLiveClient,
    live_config: PortalLiveConfig,
) -> None:
    binding = live_portal.provision_tenant(live_config.org_a_id)
    assert binding.job_id == live_config.job_id
    assert binding.organization_id == live_config.org_a_id

    audit_before = live_portal.provider_audit()
    assert audit_before.correlation_id == live_config.correlation_id
    surface_before = live_portal.get_surface(
        access_token=live_config.org_a_access_token
    )
    ticket = live_portal.issue_ticket(
        alias=live_config.bearer_alias,
        action="discover",
    )
    denied = live_portal.portal_status(
        "POST",
        f"/v1/portal/integration-setups/{live_config.job_id}/discover",
        access_token=live_config.org_b_access_token,
        payload=_ticket_payload(ticket),
    )
    assert denied.status_code == 403
    cross_read = live_portal.portal_status(
        "GET",
        f"/v1/portal/integration-setups/{live_config.job_id}",
        access_token=live_config.org_b_access_token,
    )
    assert cross_read.status_code == 404
    audit_after = live_portal.provider_audit()
    surface_after = live_portal.get_surface(
        access_token=live_config.org_a_access_token
    )
    assert audit_after.correlation_id == live_config.correlation_id
    assert audit_after.invocation_count == audit_before.invocation_count
    assert surface_after.revision == surface_before.revision


def test_ticket_lifecycle_provider_traces_restart_and_release_evidence(
    live_portal: PortalLiveClient,
    live_config: PortalLiveConfig,
) -> None:
    revisions: list[int] = []

    bearer_ticket = live_portal.issue_ticket(
        alias=live_config.bearer_alias,
        action="discover",
    )
    wrong_action = live_portal.portal_status(
        "POST",
        f"/v1/portal/integration-setups/{live_config.job_id}/actions",
        access_token=live_config.org_a_access_token,
        payload={**_ticket_payload(bearer_ticket), "action": "revoked"},
    )
    assert wrong_action.status_code == 403
    bearer_discovered = live_portal.discover(
        bearer_ticket,
        access_token=live_config.org_a_access_token,
    )
    revisions.append(bearer_discovered.revision)
    replay = live_portal.portal_status(
        "POST",
        f"/v1/portal/integration-setups/{live_config.job_id}/discover",
        access_token=live_config.org_a_access_token,
        payload=_ticket_payload(bearer_ticket),
    )
    assert replay.status_code == 403
    bearer_action = _find_action(bearer_discovered, live_config.bearer_alias)
    assert live_config.bearer_credential_id in {
        candidate.credential_id for candidate in bearer_action.candidate_credentials
    }

    bearer_select_ticket = live_portal.issue_ticket(
        alias=live_config.bearer_alias,
        action="select",
    )
    bearer_selected = live_portal.select(
        bearer_select_ticket,
        live_config.bearer_credential_id,
    )
    revisions.append(bearer_selected.revision)

    oauth_ticket = live_portal.issue_ticket(
        alias=live_config.oauth_alias,
        action="discover",
    )
    oauth_discovered = live_portal.discover(
        oauth_ticket,
        access_token=live_config.org_a_access_token,
    )
    revisions.append(oauth_discovered.revision)
    oauth_action = _find_action(oauth_discovered, live_config.oauth_alias)
    assert live_config.oauth_credential_id in {
        candidate.credential_id for candidate in oauth_action.candidate_credentials
    }
    oauth_select_ticket = live_portal.issue_ticket(
        alias=live_config.oauth_alias,
        action="select",
    )
    oauth_selected = live_portal.select(
        oauth_select_ticket,
        live_config.oauth_credential_id,
    )
    revisions.append(oauth_selected.revision)

    restart = live_portal.restart_and_resume()
    assert restart.correlation_id == live_config.correlation_id
    resumed = live_portal.get_surface(access_token=live_config.org_a_access_token)
    assert resumed.revision == oauth_selected.revision
    consumed_after_restart = live_portal.portal_status(
        "POST",
        f"/v1/portal/integration-setups/{live_config.job_id}/discover",
        access_token=live_config.org_a_access_token,
        payload=_ticket_payload(oauth_ticket),
    )
    assert consumed_after_restart.status_code == 403

    traces = (
        live_portal.provider_probe("bearer"),
        live_portal.provider_probe("oauth"),
        live_portal.provider_probe("bearer"),
    )
    assert all(trace.correlation_id == live_config.correlation_id for trace in traces)
    expected_trace_bindings = (
        (
            "bearer",
            live_config.bearer_alias,
            live_config.bearer_credential_id,
        ),
        (
            "oauth",
            live_config.oauth_alias,
            live_config.oauth_credential_id,
        ),
        (
            "bearer",
            live_config.bearer_alias,
            live_config.bearer_credential_id,
        ),
    )
    assert tuple(
        (trace.integration_kind, trace.credential_alias, trace.credential_id)
        for trace in traces
    ) == expected_trace_bindings
    assert len({trace.trace_id for trace in traces}) == 3
    oauth_trace = traces[1]
    assert oauth_trace.oauth_grant_type == "client_credentials"
    assert oauth_trace.oauth_exchange_id is not None
    assert oauth_trace.oauth_exchange_ref is not None

    release = live_portal.release_evidence()
    assert release.correlation_id == live_config.correlation_id
    assert all(
        trace.correlation_id == live_config.correlation_id
        for trace in release.provider_traces
    )
    assert len(release.provider_traces) == 3
    assert len({trace.trace_id for trace in release.provider_traces}) == 3
    release_traces = {trace.trace_id: trace for trace in release.provider_traces}
    requested_traces = {trace.trace_id: trace for trace in traces}
    assert release_traces == requested_traces
    assert release.gitea_sha256
    assert release.gateway_decision_ref
    assert release.gateway_execution_ref
    assert release.minibook_projection_ref
    assert release.minibook_rebuild_ref

    rotation_ticket = live_portal.issue_ticket(
        alias=live_config.bearer_alias,
        action="rotation_requested",
    )
    rotated = live_portal.action(rotation_ticket, "rotation_requested")
    revisions.append(rotated.revision)
    revoke_ticket = live_portal.issue_ticket(
        alias=live_config.bearer_alias,
        action="revoked",
    )
    revoked = live_portal.action(revoke_ticket, "revoked")
    revisions.append(revoked.revision)
    assert _find_action(revoked, live_config.bearer_alias).status == "revoked"
    _require_monotonic(revisions)
