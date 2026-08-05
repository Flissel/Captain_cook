"""Real, disposable portal gate; never falls back to mocks."""

from __future__ import annotations

from contextlib import closing
from typing import Any, Mapping

import pytest

from tests.live.portal_live_support import (
    PortalLiveClient,
    PortalLiveConfig,
    PortalLiveConfigurationError,
    require_secret_free_surface,
    require_status,
)


pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def live_config() -> PortalLiveConfig:
    try:
        return PortalLiveConfig.from_environment()
    except PortalLiveConfigurationError as error:
        pytest.skip(f"missing-live-prerequisites: {error}")


@pytest.fixture(scope="module")
def live_portal(live_config: PortalLiveConfig) -> PortalLiveClient:
    with closing(PortalLiveClient(live_config)) as client:
        yield client


def _path(config: PortalLiveConfig, suffix: str = "") -> str:
    return f"/v1/portal/integration-setups/{config.job_id}{suffix}"


def _issue(
    client: PortalLiveClient,
    config: PortalLiveConfig,
    *,
    alias: str,
    action: str,
) -> Mapping[str, Any]:
    response = client.portal(
        "POST",
        _path(config, "/tickets"),
        access_token=config.org_a_access_token,
        payload={"credential_alias": alias, "action": action},
    )
    return require_status(response, 201)


def _ticket_use(ticket: Mapping[str, Any], alias: str) -> dict[str, Any]:
    return {
        "ticket_id": ticket["ticket_id"],
        "ticket": ticket["ticket"],
        "credential_alias": alias,
    }


def _revision(payload: Mapping[str, Any]) -> int:
    revision = payload.get("revision")
    assert isinstance(revision, int) and revision >= 1
    return revision


def _find_action(payload: Mapping[str, Any], alias: str) -> Mapping[str, Any]:
    actions = payload.get("actions")
    assert isinstance(actions, list)
    match = [item for item in actions if isinstance(item, dict) and item.get("credential_alias") == alias]
    assert len(match) == 1
    return match[0]


def test_live_references_are_reachable_without_forwarding_credentials(
    live_portal: PortalLiveClient,
    live_config: PortalLiveConfig,
) -> None:
    statuses = tuple(
        live_portal.health(url)
        for url in (
            live_config.n8n_health_url,
            live_config.gitea_health_url,
            live_config.supabase_health_url,
            live_config.minibook_health_url,
        )
    )
    assert all(200 <= status < 400 for status in statuses)


def test_portal_rejects_cross_tenant_ticket_before_provider_call(
    live_portal: PortalLiveClient,
    live_config: PortalLiveConfig,
) -> None:
    binding = live_portal.portal(
        "POST",
        f"/v1/factory/integration-setups/{live_config.job_id}/portal-tenant-binding",
        access_token=live_config.captain_token,
        payload={"job_id": str(live_config.job_id), "organization_id": live_config.org_a_id},
    )
    require_status(binding, 200, 201)
    conflicting_binding = live_portal.portal(
        "POST",
        f"/v1/factory/integration-setups/{live_config.job_id}/portal-tenant-binding",
        access_token=live_config.captain_token,
        payload={"job_id": str(live_config.job_id), "organization_id": live_config.org_b_id},
    )
    assert conflicting_binding.status_code == 409
    before = require_status(
        live_portal.portal(
            "GET",
            _path(live_config),
            access_token=live_config.org_a_access_token,
        ),
        200,
    )
    ticket = _issue(
        live_portal,
        live_config,
        alias=live_config.bearer_alias,
        action="discover",
    )
    denied = live_portal.portal(
        "POST",
        _path(live_config, "/discover"),
        access_token=live_config.org_b_access_token,
        payload=_ticket_use(ticket, live_config.bearer_alias),
    )
    assert denied.status_code == 403
    cross_read = live_portal.portal(
        "GET",
        _path(live_config),
        access_token=live_config.org_b_access_token,
    )
    assert cross_read.status_code == 404
    after = require_status(
        live_portal.portal(
            "GET",
            _path(live_config),
            access_token=live_config.org_a_access_token,
        ),
        200,
    )
    assert _revision(after) == _revision(before)


def test_live_ticket_binding_bearer_oauth_rotation_and_revoke(
    live_portal: PortalLiveClient,
    live_config: PortalLiveConfig,
) -> None:
    revisions: list[int] = []

    bearer_ticket = _issue(
        live_portal,
        live_config,
        alias=live_config.bearer_alias,
        action="discover",
    )
    wrong_action = live_portal.portal(
        "POST",
        _path(live_config, "/actions"),
        access_token=live_config.org_a_access_token,
        payload={
            **_ticket_use(bearer_ticket, live_config.bearer_alias),
            "action": "revoked",
        },
    )
    assert wrong_action.status_code == 403
    bearer_discovered = require_status(
        live_portal.portal(
            "POST",
            _path(live_config, "/discover"),
            access_token=live_config.org_a_access_token,
            payload=_ticket_use(bearer_ticket, live_config.bearer_alias),
        ),
        200,
    )
    require_secret_free_surface(bearer_discovered)
    revisions.append(_revision(bearer_discovered))
    replay = live_portal.portal(
        "POST",
        _path(live_config, "/discover"),
        access_token=live_config.org_a_access_token,
        payload=_ticket_use(bearer_ticket, live_config.bearer_alias),
    )
    assert replay.status_code == 403
    bearer_action = _find_action(bearer_discovered, live_config.bearer_alias)
    candidates = bearer_action.get("candidate_credentials")
    assert isinstance(candidates, list)
    assert any(
        isinstance(candidate, dict)
        and candidate.get("credential_id") == live_config.bearer_credential_id
        for candidate in candidates
    )

    bearer_select_ticket = _issue(
        live_portal,
        live_config,
        alias=live_config.bearer_alias,
        action="select",
    )
    bearer_selected = require_status(
        live_portal.portal(
            "POST",
            _path(live_config, "/select"),
            access_token=live_config.org_a_access_token,
            payload={
                **_ticket_use(bearer_select_ticket, live_config.bearer_alias),
                "credential_id": live_config.bearer_credential_id,
            },
        ),
        200,
    )
    require_secret_free_surface(bearer_selected)
    revisions.append(_revision(bearer_selected))

    oauth_ticket = _issue(
        live_portal,
        live_config,
        alias=live_config.oauth_alias,
        action="discover",
    )
    oauth_discovered = require_status(
        live_portal.portal(
            "POST",
            _path(live_config, "/discover"),
            access_token=live_config.org_a_access_token,
            payload=_ticket_use(oauth_ticket, live_config.oauth_alias),
        ),
        200,
    )
    require_secret_free_surface(oauth_discovered)
    revisions.append(_revision(oauth_discovered))
    oauth_action = _find_action(oauth_discovered, live_config.oauth_alias)
    oauth_candidates = oauth_action.get("candidate_credentials")
    assert isinstance(oauth_candidates, list)
    assert any(
        isinstance(candidate, dict)
        and candidate.get("credential_id") == live_config.oauth_credential_id
        for candidate in oauth_candidates
    )

    oauth_select_ticket = _issue(
        live_portal,
        live_config,
        alias=live_config.oauth_alias,
        action="select",
    )
    oauth_selected = require_status(
        live_portal.portal(
            "POST",
            _path(live_config, "/select"),
            access_token=live_config.org_a_access_token,
            payload={
                **_ticket_use(oauth_select_ticket, live_config.oauth_alias),
                "credential_id": live_config.oauth_credential_id,
            },
        ),
        200,
    )
    revisions.append(_revision(oauth_selected))

    rotation_ticket = _issue(
        live_portal,
        live_config,
        alias=live_config.bearer_alias,
        action="rotation_requested",
    )
    rotated = require_status(
        live_portal.portal(
            "POST",
            _path(live_config, "/actions"),
            access_token=live_config.org_a_access_token,
            payload={
                **_ticket_use(rotation_ticket, live_config.bearer_alias),
                "action": "rotation_requested",
            },
        ),
        200,
    )
    revisions.append(_revision(rotated))

    revoke_ticket = _issue(
        live_portal,
        live_config,
        alias=live_config.bearer_alias,
        action="revoked",
    )
    revoked = require_status(
        live_portal.portal(
            "POST",
            _path(live_config, "/actions"),
            access_token=live_config.org_a_access_token,
            payload={
                **_ticket_use(revoke_ticket, live_config.bearer_alias),
                "action": "revoked",
            },
        ),
        200,
    )
    revisions.append(_revision(revoked))
    assert _find_action(revoked, live_config.bearer_alias).get("status") == "revoked"
    assert all(current > previous for previous, current in zip(revisions, revisions[1:]))


def test_provider_and_release_evidence_requires_a_safe_callable_seam(
    live_config: PortalLiveConfig,
) -> None:
    assert live_config.oauth_client_id
    assert live_config.oauth_auth_url
    assert live_config.oauth_callback_url
    pytest.skip(
        "BLOCKED-LIVE: no safe callable portal seam currently exposes Bearer probe "
        "verification, OAuth consent/callback verification, controlled Gateway/portal "
        "restart-resume, three provider traces, Gitea release digest, accepted Gateway "
        "decision/execution reference, or Minibook projection/rebuild for the configured "
        "correlation_id"
    )
