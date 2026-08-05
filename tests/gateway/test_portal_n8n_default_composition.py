from __future__ import annotations

import json
import ssl
from types import SimpleNamespace

import certifi
import pytest

from gateway import portal_n8n_composition
from gateway.portal_n8n_adapters import (
    PortalN8nCredentialMetadataSource,
    PortalN8nCredentialVerificationSource,
)
from gateway.portal_n8n_composition import (
    ConfiguredVerificationReleaseSource,
    build_portal_n8n_adapter_bundle,
)
from gateway.app import create_app
from gateway.settings import GatewaySettings
from tests.gateway.test_gateway_settings import valid_environment


def _settings() -> GatewaySettings:
    revision = "1" * 40
    releases = json.dumps(
        [
            {
                "repository": "captain/templates",
                "revision": revision,
                "path": "verification/bearer.json",
                "contents_url": (
                    "https://gitea.example.test/captain/templates/raw/commit/"
                    f"{revision}/verification/bearer.json"
                ),
                "sha256": "a" * 64,
            }
        ]
    )
    return GatewaySettings.from_env(
        valid_environment(
            CAPTAIN_PORTAL_N8N_ADAPTERS_ENABLED="true",
            CAPTAIN_N8N_API_KEY="api-test-secret",
            CAPTAIN_N8N_MCP_TOKEN="mcp-test-secret",
            CAPTAIN_PORTAL_GITEA_ORIGIN="https://gitea.example.test",
            CAPTAIN_PORTAL_VERIFICATION_RELEASES_JSON=releases,
        )
    )


class NeverCalledReader:
    def factory_job(self, job_id):
        raise AssertionError(f"unexpected factory job read: {job_id}")


def test_default_bundle_builds_both_lease_bound_adapters_without_secret_repr() -> None:
    settings = _settings()

    bundle = build_portal_n8n_adapter_bundle(
        reader=NeverCalledReader(),
        settings=settings,
    )

    assert isinstance(bundle.credential_source, PortalN8nCredentialMetadataSource)
    assert isinstance(
        bundle.verification_source,
        PortalN8nCredentialVerificationSource,
    )
    assert "api-test-secret" not in repr(bundle)
    assert "mcp-test-secret" not in repr(bundle)


def test_private_https_clients_use_the_explicit_gateway_ca_bundle() -> None:
    settings = _settings().model_copy(
        update={"tls_ca_bundle_path": certifi.where()}
    )

    assert isinstance(portal_n8n_composition._tls_verify(settings), ssl.SSLContext)


def test_configured_release_source_rejects_every_unpinned_digest() -> None:
    settings = _settings()
    releases = ConfiguredVerificationReleaseSource(
        settings.portal_verification_releases
    )

    assert releases.by_sha256("a" * 64) == settings.portal_verification_releases[0]

    with pytest.raises(
        ValueError,
        match="verification workflow release is not configured",
    ):
        releases.by_sha256("b" * 64)


def test_gateway_default_composition_installs_the_complete_bundle(monkeypatch) -> None:
    instances = []
    credential_source = object()
    verification_source = object()

    class RecordingStore:
        def __init__(self, storage, **kwargs):
            self.storage = storage
            self.kwargs = kwargs
            self.configured = None
            instances.append(self)

        def configure_portal_sources(self, **kwargs):
            self.configured = kwargs

    monkeypatch.setattr("gateway.app.GatewayStore", RecordingStore)
    monkeypatch.setattr(
        "gateway.app.build_portal_n8n_adapter_bundle",
        lambda **kwargs: SimpleNamespace(
            credential_source=credential_source,
            verification_source=verification_source,
        ),
    )

    create_app(storage=object(), settings=_settings())

    assert len(instances) == 1
    assert instances[0].configured == {
        "credential_source": credential_source,
        "verification_source": verification_source,
    }
