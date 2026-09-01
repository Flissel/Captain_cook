"""Fail-closed process composition for the authenticated runtime boundary."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import UUID

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agenten.agent_runtime.capabilities import derive_grant, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    CapabilityGrantRevocation,
    RuntimeResumeCostAuthorityV1,
    RuntimeResumeCostSettlementRequestV1,
    RuntimeResumeCostSettlementV1,
)
from agenten.agent_runtime.gateway_client import (
    GatewayRuntimeClient,
    GatewayRuntimeError,
)
from agenten.agent_runtime.http_server import create_runtime_app
from agenten.agent_runtime.ports import (
    ArtifactPort,
    CodexExecutionPort,
    HermesPlannerPort,
)
from agenten.agent_runtime.production_bootstrap import (
    RuntimeBootstrap,
    load_runtime_adapters_from_env,
)
from agenten.agent_runtime.service import AgentRuntimeService
from agenten.validation.contracts import WorkBatch


class RuntimeConfigurationError(ValueError):
    """Runtime configuration is absent or cannot produce an authoritative service."""


class RuntimeEntrypointSettings(BaseModel):
    """Strict settings whose representation never exposes bearer credentials."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    runtime_token: SecretStr
    gateway_token: SecretStr
    gateway_url: str = Field(pattern=r"^https?://[^\s/]+(?::[0-9]+)?(?:/[^\s]*)?$")
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8091, ge=1, le=65535)
    provider_proxy_url: str | None = None

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> "RuntimeEntrypointSettings":
        source = os.environ if environ is None else environ
        required = (
            "CAPTAIN_RUNTIME_TOKEN",
            "CAPTAIN_GATEWAY_TOKEN",
            "CAPTAIN_GATEWAY_URL",
        )
        missing = [
            name
            for name in required
            if not isinstance(source.get(name), str) or not source[name].strip()
        ]
        if missing:
            raise RuntimeConfigurationError(
                f"missing required runtime settings: {', '.join(missing)}"
            )
        try:
            port = int(source.get("CAPTAIN_RUNTIME_PORT", "8091"))
            return cls(
                runtime_token=SecretStr(source["CAPTAIN_RUNTIME_TOKEN"]),
                gateway_token=SecretStr(source["CAPTAIN_GATEWAY_TOKEN"]),
                gateway_url=source["CAPTAIN_GATEWAY_URL"],
                port=port,
                provider_proxy_url=source.get("CAPTAIN_PROVIDER_PROXY_URL") or None,
            )
        except (TypeError, ValueError, ValidationError):
            raise RuntimeConfigurationError("invalid runtime configuration") from None


class GatewayBackedRuntimeState:
    """Complete runtime state port backed only by Captain's authenticated Gateway."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client
        self._runtime = GatewayRuntimeClient(base_url, token, client)

    async def accept_command(self, command: AgentRuntimeCommand) -> None:
        await self._runtime.accept_command(command)

    async def get_released_batch(self, command: AgentRuntimeCommand) -> WorkBatch:
        batch_id = command.payload.batch_id
        if batch_id is None:
            raise GatewayRuntimeError("runtime command has no released batch binding")
        try:
            response = await self._client.get(
                f"{self._base_url}/batches/{quote(batch_id, safe='')}/bundle",
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError:
            raise GatewayRuntimeError("read released batch could not reach the gateway") from None
        if response.status_code != 200:
            raise GatewayRuntimeError(
                f"read released batch failed with gateway status {response.status_code}"
            )
        try:
            batch = WorkBatch.model_validate(response.json())
        except (TypeError, ValueError, ValidationError):
            raise GatewayRuntimeError("read released batch returned an invalid response") from None
        if batch.batch_id != batch_id:
            raise GatewayRuntimeError("released batch does not match the runtime command")
        return batch

    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None:
        return await self._runtime.get_grant(command_id)

    async def get_grant_revocation(
        self,
        command_id: UUID,
    ) -> CapabilityGrantRevocation | None:
        return await self._runtime.get_grant_revocation(command_id)

    async def record_grant(self, grant: CapabilityGrant) -> CapabilityGrant:
        return await self._runtime.record_grant(grant)

    async def get_result(self, command_id: UUID) -> AgentRuntimeResult | None:
        return await self._runtime.get_result(command_id)

    async def record_result(self, result: AgentRuntimeResult) -> AgentRuntimeResult:
        return await self._runtime.record_result(result)


class GatewayBackedRuntimeCostAuthority:
    """Worker-authenticated Gateway authority and atomic resume accounting."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient,
        provider_finalizer: "ProviderProxyRuntimeFinalizer | None" = None,
    ) -> None:
        if not base_url.strip() or not token:
            raise ValueError("Gateway cost authority configuration is incomplete")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client
        self._provider_finalizer = provider_finalizer

    async def authorize(
        self, command: AgentRuntimeCommand
    ) -> RuntimeResumeCostAuthorityV1:
        return await self._post(
            "/v1/capability-resumes/cost-authorities/validate",
            command.model_dump(mode="json", by_alias=True),
            RuntimeResumeCostAuthorityV1,
            operation="validate resume cost authority",
        )

    async def settle(
        self,
        command: AgentRuntimeCommand,
        result: AgentRuntimeResult,
        authority: RuntimeResumeCostAuthorityV1,
    ) -> RuntimeResumeCostSettlementV1:
        if self._provider_finalizer is not None:
            await self._provider_finalizer.finalize(command, result)
        request = RuntimeResumeCostSettlementRequestV1(
            command=command,
            result=result,
            authority=authority,
        )
        return await self._post(
            "/v1/capability-resumes/cost-authorities/settle",
            request.model_dump(mode="json", by_alias=True),
            RuntimeResumeCostSettlementV1,
            operation="settle resume cost authority",
        )

    async def _post(self, path, payload, model, *, operation):
        try:
            response = await self._client.post(
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                json=payload,
            )
        except httpx.HTTPError:
            raise GatewayRuntimeError(
                f"{operation} could not reach the gateway"
            ) from None
        if response.status_code != 200:
            raise GatewayRuntimeError(
                f"{operation} failed with gateway status {response.status_code}"
            )
        try:
            return model.model_validate(response.json())
        except (TypeError, ValueError, ValidationError):
            raise GatewayRuntimeError(
                f"{operation} returned an invalid response"
            ) from None


class ProviderProxyRuntimeFinalizer:
    """Runtime-scoped trigger; the proxy alone derives and writes provider truth."""

    def __init__(self, *, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.port is None
            or parsed.path not in {"", "/"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not token.strip()
        ):
            raise ValueError("provider proxy finalizer must use authenticated loopback")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def finalize(
        self, command: AgentRuntimeCommand, result: AgentRuntimeResult
    ) -> None:
        try:
            response = await self._client.post(
                f"{self._base_url}/v1/captain/provider-settlements/finalize",
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "command_id": str(command.event_id),
                    "result_id": str(result.event_id),
                },
            )
        except httpx.HTTPError:
            raise GatewayRuntimeError(
                "finalize provider settlement could not reach the proxy"
            ) from None
        if response.status_code != 200:
            raise GatewayRuntimeError(
                f"finalize provider settlement failed with proxy status {response.status_code}"
            )


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class _CaptainCapabilityPolicy:
    def derive(
        self,
        command: AgentRuntimeCommand,
        batch: WorkBatch | None,
        now: datetime,
    ) -> CapabilityGrant:
        return derive_grant(command, batch, now)

    def validate(
        self,
        grant: CapabilityGrant,
        command: AgentRuntimeCommand,
        now: datetime,
        revocation: CapabilityGrantRevocation | None = None,
    ) -> CapabilityGrant:
        return validate_grant(grant, command, now, revocation)


def compose_gateway_backed_runtime_app(
    *,
    settings: RuntimeEntrypointSettings,
    client: httpx.AsyncClient,
    hermes: HermesPlannerPort,
    codex: CodexExecutionPort,
    artifacts: ArtifactPort,
) -> FastAPI:
    """Compose existing real adapters around Gateway-owned runtime state."""

    gateway_token = settings.gateway_token.get_secret_value()
    service = AgentRuntimeService(
        state=GatewayBackedRuntimeState(
            base_url=settings.gateway_url,
            token=gateway_token,
            client=client,
        ),
        hermes=hermes,
        codex=codex,
        artifacts=artifacts,
        capabilities=_CaptainCapabilityPolicy(),
        clock=_UtcClock(),
        cost_authority=GatewayBackedRuntimeCostAuthority(
            base_url=settings.gateway_url,
            token=gateway_token,
            client=client,
            provider_finalizer=(
                ProviderProxyRuntimeFinalizer(
                    base_url=settings.provider_proxy_url,
                    token=settings.runtime_token.get_secret_value(),
                    client=client,
                )
                if settings.provider_proxy_url is not None
                else None
            ),
        ),
    )
    return create_runtime_app(
        executor=service,
        token=settings.runtime_token.get_secret_value(),
    )


def _repository_root() -> Path:
    return Path(__file__).resolve().parents[2]


def preflight_runtime() -> RuntimeBootstrap:
    """Validate settings and digest-bound ports before any listener starts."""

    settings = RuntimeEntrypointSettings.from_env()
    binding = load_runtime_adapters_from_env(repository_root=_repository_root())
    return RuntimeBootstrap(
        settings=settings,
        binding=binding,
    )


def main() -> None:
    """Compose and launch the authenticated runtime from verified adapters."""

    bootstrap = preflight_runtime()
    client = httpx.AsyncClient()
    app = compose_gateway_backed_runtime_app(
        settings=bootstrap.settings,
        client=client,
        hermes=bootstrap.binding.hermes,
        codex=bootstrap.binding.codex,
        artifacts=bootstrap.binding.artifacts,
    )
    uvicorn.run(
        app,
        host=bootstrap.settings.host,
        port=bootstrap.settings.port,
        workers=1,
    )


if __name__ == "__main__":
    main()
