"""Fail-closed default composition for Captain-owned portal n8n adapters."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import ssl

import httpx

from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from agenten.agent_factory.gitea_templates import (
    GiteaTemplateClient,
    VerifiedTemplatePayload,
)
from agenten.agent_factory.integration_setup_n8n import (
    CaptainN8nCredentialMetadataClient,
)
from agenten.agent_factory.contracts import FactoryLease
from agenten.agent_factory.integration_setup import (
    IntegrationCredentialRequirementV1,
    N8nCredentialMetadataV1,
)
from agenten.agent_runtime.n8n_endpoint import N8nEndpoint, resolve_n8n_endpoint
from agenten.targets.n8n import (
    N8nDeployment,
    N8nExecutionEvidence,
    N8nHttpClient,
    N8nTarget,
    SealedArtifact,
    ValidationCase,
)
from gateway.portal_n8n_adapters import (
    FactoryJobReader,
    GatewayPortalN8nLeaseSource,
    PortalN8nCredentialMetadataSource,
    PortalN8nCredentialVerificationSource,
)
from gateway.settings import GatewaySettings


class ConfiguredVerificationReleaseSource:
    """Resolve only immutable releases declared at Gateway boot."""

    def __init__(self, releases: tuple[GiteaTemplateReleaseV1, ...]) -> None:
        if not releases:
            raise ValueError("at least one portal verification release is required")
        self._by_digest = {release.sha256: release for release in releases}
        if len(self._by_digest) != len(releases):
            raise ValueError("portal verification release digests must be unique")

    def by_sha256(self, sha256: str) -> GiteaTemplateReleaseV1:
        try:
            return self._by_digest[sha256]
        except KeyError:
            raise ValueError("verification workflow release is not configured") from None


def _tls_verify(settings: GatewaySettings) -> ssl.SSLContext | bool:
    if settings.tls_ca_bundle_path is None:
        return True
    return ssl.create_default_context(cafile=settings.tls_ca_bundle_path)


class _BoundedMetadataClient:
    def __init__(self, verify: ssl.SSLContext | bool) -> None:
        self._verify = verify

    async def discover(
        self,
        *,
        lease: FactoryLease,
        endpoint: N8nEndpoint,
        requirement: IntegrationCredentialRequirementV1,
        now: datetime,
        timeout_seconds: float,
    ) -> tuple[N8nCredentialMetadataV1, ...]:
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            verify=self._verify,
        ) as http:
            return await CaptainN8nCredentialMetadataClient(http=http).discover(
                lease=lease,
                endpoint=endpoint,
                requirement=requirement,
                now=now,
                timeout_seconds=timeout_seconds,
            )


class _BoundedTemplateSource:
    def __init__(self, origin: str, verify: ssl.SSLContext | bool) -> None:
        self._origin = origin
        self._verify = verify

    async def fetch_verified_payload(
        self,
        release: GiteaTemplateReleaseV1,
    ) -> VerifiedTemplatePayload:
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            verify=self._verify,
        ) as http:
            return await GiteaTemplateClient(
                origin=self._origin,
                http=http,
            ).fetch_verified_payload(release)


class _BoundedN8nTarget:
    def __init__(
        self,
        endpoint: N8nEndpoint,
        verify: ssl.SSLContext | bool,
    ) -> None:
        self._endpoint = endpoint
        self._verify = verify

    def _target(self, http: httpx.AsyncClient) -> N8nTarget:
        return N8nTarget(N8nHttpClient.from_endpoint(self._endpoint, http))

    async def deploy(self, artifact: SealedArtifact) -> N8nDeployment:
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            verify=self._verify,
        ) as http:
            return await self._target(http).deploy(artifact)

    async def execute(
        self,
        deployment: N8nDeployment,
        case: ValidationCase,
    ) -> N8nExecutionEvidence:
        async with httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            verify=self._verify,
        ) as http:
            return await self._target(http).execute(deployment, case)


@dataclass(frozen=True, repr=False)
class PortalN8nAdapterBundle:
    credential_source: PortalN8nCredentialMetadataSource
    verification_source: PortalN8nCredentialVerificationSource


def build_portal_n8n_adapter_bundle(
    *,
    reader: FactoryJobReader,
    settings: GatewaySettings,
) -> PortalN8nAdapterBundle:
    """Build both adapters or fail before the Gateway starts serving traffic."""

    if not settings.portal_n8n_adapters_configured:
        raise ValueError("portal n8n adapters are not configured")
    assert settings.portal_n8n_api_key is not None
    assert settings.portal_n8n_mcp_token is not None
    assert settings.portal_gitea_origin is not None
    endpoint = resolve_n8n_endpoint(
        {
            "N8N_MODE": "captain-builder",
            "CAPTAIN_N8N_URL": settings.captain_n8n_ui_url,
            "CAPTAIN_N8N_API_KEY": settings.portal_n8n_api_key.get_secret_value(),
            "CAPTAIN_N8N_MCP_TOKEN": settings.portal_n8n_mcp_token.get_secret_value(),
        }
    )
    verify = _tls_verify(settings)
    leases = GatewayPortalN8nLeaseSource(reader)
    return PortalN8nAdapterBundle(
        credential_source=PortalN8nCredentialMetadataSource(
            leases=leases,
            client=_BoundedMetadataClient(verify),
            endpoint=endpoint,
        ),
        verification_source=PortalN8nCredentialVerificationSource(
            leases=leases,
            releases=ConfiguredVerificationReleaseSource(
                settings.portal_verification_releases
            ),
            templates=_BoundedTemplateSource(settings.portal_gitea_origin, verify),
            target=_BoundedN8nTarget(endpoint, verify),
        ),
    )
