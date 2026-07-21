"""Fail-closed process composition for the authenticated runtime boundary."""

from __future__ import annotations

import os
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote
from uuid import UUID

import httpx
from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError

from agenten.agent_runtime.capabilities import derive_grant, validate_grant
from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
    CapabilityGrantRevocation,
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


class _UtcClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class _CaptainCapabilityPolicy:
    def derive(
        self,
        command: AgentRuntimeCommand,
        batch: WorkBatch,
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
    )
    return create_runtime_app(
        executor=service,
        token=settings.runtime_token.get_secret_value(),
    )


def main() -> None:
    """Fail closed until production Hermes, Codex, and artifact ports are installed."""

    RuntimeEntrypointSettings.from_env()
    raise RuntimeConfigurationError(
        "production Hermes, Codex, and artifact runtime ports are unavailable"
    )


if __name__ == "__main__":
    main()
