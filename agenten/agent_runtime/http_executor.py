"""Authenticated HTTP executor for a separately hosted Agent Runtime service."""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import ValidationError

from agenten.agent_runtime.contracts import AgentRuntimeCommand, AgentRuntimeResult


class AgentRuntimeHttpError(RuntimeError):
    """The remote runtime failed without retaining response or credential data."""


class AgentRuntimeHttpExecutor:
    """Execute one typed command through a real HTTP runtime boundary."""

    def __init__(self, base_url: str, token: str, client: httpx.AsyncClient) -> None:
        if not base_url.strip():
            raise ValueError("runtime base_url must not be empty")
        if not token:
            raise ValueError("runtime token must not be empty")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def execute(self, command: AgentRuntimeCommand) -> AgentRuntimeResult:
        try:
            response = await self._client.request(
                "POST",
                f"{self._base_url}/v1/runtime/execute",
                headers={"Authorization": f"Bearer {self._token}"},
                json=command.model_dump(mode="json", by_alias=True),
            )
        except httpx.HTTPError:
            raise AgentRuntimeHttpError("runtime command could not reach the service") from None
        if response.status_code != 200:
            raise AgentRuntimeHttpError(
                f"runtime command failed with service status {response.status_code}"
            )
        try:
            return AgentRuntimeResult.model_validate(response.json())
        except (ValueError, ValidationError):
            raise AgentRuntimeHttpError("runtime command returned an invalid response") from None
