"""Authenticated HTTP adapter for authoritative runtime operation state."""

from __future__ import annotations

from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    CapabilityGrant,
)


class GatewayRuntimeError(RuntimeError):
    """A gateway request failed without retaining response or credential data."""


class GatewayRuntimeConflictError(GatewayRuntimeError):
    """The gateway rejected a stale or conflicting runtime write."""


class RuntimeWriteReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    replayed: bool


class RuntimeOperationProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: UUID
    command: AgentRuntimeCommand
    grant: CapabilityGrant | None = None
    result: AgentRuntimeResult | None = None


class GatewayRuntimeClient:
    """Persist and read runtime commands through the sole-writer gateway."""

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        if not base_url.strip():
            raise ValueError("gateway base_url must not be empty")
        if not token:
            raise ValueError("gateway token must not be empty")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def accept_runtime_command(
        self,
        command: AgentRuntimeCommand,
    ) -> RuntimeWriteReceipt:
        return await self._write(
            "/v1/runtime/commands",
            command.model_dump(mode="json", by_alias=True),
            expected={202},
            operation="accept runtime command",
        )

    async def record_capability_grant(
        self,
        grant: CapabilityGrant,
    ) -> RuntimeWriteReceipt:
        return await self._write(
            "/v1/runtime/grants",
            grant.model_dump(mode="json", by_alias=True),
            expected={200, 201},
            operation="record capability grant",
        )

    async def record_runtime_result(
        self,
        result: AgentRuntimeResult,
    ) -> RuntimeWriteReceipt:
        return await self._write(
            "/v1/runtime/results",
            result.model_dump(mode="json", by_alias=True),
            expected={200, 201},
            operation="record runtime result",
        )

    async def get_runtime_operation(
        self,
        operation_id: UUID,
    ) -> RuntimeOperationProjection:
        response = await self._request(
            "GET",
            f"/v1/runtime/operations/{quote(str(operation_id), safe='')}",
            operation="read runtime operation",
        )
        self._require_status(response, {200}, operation="read runtime operation")
        try:
            return RuntimeOperationProjection.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayRuntimeError(
                "read runtime operation returned an invalid response"
            ) from None

    async def accept_command(self, command: AgentRuntimeCommand) -> None:
        await self.accept_runtime_command(command)

    async def record_grant(self, grant: CapabilityGrant) -> CapabilityGrant:
        await self.record_capability_grant(grant)
        return grant

    async def record_result(self, result: AgentRuntimeResult) -> AgentRuntimeResult:
        await self.record_runtime_result(result)
        return result

    async def get_grant(self, command_id: UUID) -> CapabilityGrant | None:
        return (await self.get_runtime_operation(command_id)).grant

    async def get_result(self, command_id: UUID) -> AgentRuntimeResult | None:
        return (await self.get_runtime_operation(command_id)).result

    async def _write(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        expected: set[int],
        operation: str,
    ) -> RuntimeWriteReceipt:
        response = await self._request(
            "POST",
            path,
            operation=operation,
            json=payload,
        )
        self._require_status(response, expected, operation=operation)
        try:
            return RuntimeWriteReceipt.model_validate(response.json())
        except (ValueError, ValidationError):
            raise GatewayRuntimeError(f"{operation} returned an invalid response") from None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> httpx.Response:
        try:
            return await self._client.request(
                method,
                f"{self._base_url}{path}",
                headers=self._headers,
                **kwargs,
            )
        except httpx.HTTPError:
            raise GatewayRuntimeError(f"{operation} could not reach the gateway") from None

    @staticmethod
    def _require_status(
        response: httpx.Response,
        expected: set[int],
        *,
        operation: str,
    ) -> None:
        if response.status_code == 409:
            raise GatewayRuntimeConflictError(
                f"{operation} failed with gateway status 409"
            )
        if response.status_code not in expected:
            raise GatewayRuntimeError(
                f"{operation} failed with gateway status {response.status_code}"
            )
