"""Authenticated HTTP client for persistent capability resume authority."""

from __future__ import annotations

from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from gateway.capability_live_pipeline import (
    CapabilityExecutionAuthority,
    CapabilityResumeAuthorization,
    CapabilityResumeExecutionRequest,
)


ResumeResponse = TypeVar("ResumeResponse", bound=BaseModel)


class GatewayCapabilityResumeClientError(RuntimeError):
    """A sanitized Gateway transport or contract failure."""


class GatewayCapabilityResumeClient:
    """Implement the capability-live resume port over Captain-authenticated HTTP."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        client: httpx.AsyncClient,
    ) -> None:
        if not base_url.strip():
            raise ValueError("gateway base_url must not be empty")
        if not token or "\r" in token or "\n" in token:
            raise ValueError("gateway token must be a valid HTTP credential")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    async def authorize_resume(
        self,
        request: CapabilityResumeExecutionRequest,
    ) -> CapabilityResumeAuthorization:
        return await self._post(
            "/v1/capability-resumes/authorizations",
            request,
            CapabilityResumeAuthorization,
            expected={200, 201},
            operation="authorize capability resume",
        )

    async def dispatch_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority:
        return await self._post(
            "/v1/capability-resumes/dispatches",
            authorization,
            CapabilityExecutionAuthority,
            expected={200, 201},
            operation="dispatch capability resume",
        )

    async def readback_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority:
        return await self._post(
            "/v1/capability-resumes/readbacks",
            authorization,
            CapabilityExecutionAuthority,
            expected={200},
            operation="read back capability resume",
        )

    async def _post(
        self,
        path: str,
        payload: BaseModel,
        model: type[ResumeResponse],
        *,
        expected: set[int],
        operation: str,
    ) -> ResumeResponse:
        try:
            response = await self._client.request(
                "POST",
                f"{self._base_url}{path}",
                headers={"Authorization": f"Bearer {self._token}"},
                json=payload.model_dump(mode="json", by_alias=True),
            )
        except httpx.HTTPError:
            raise GatewayCapabilityResumeClientError(
                f"{operation} could not reach the gateway"
            ) from None
        if response.status_code not in expected:
            raise GatewayCapabilityResumeClientError(
                f"{operation} failed with gateway status {response.status_code}"
            )
        try:
            return model.model_validate(response.json(), strict=False)
        except (ValueError, ValidationError):
            raise GatewayCapabilityResumeClientError(
                f"{operation} returned an invalid response"
            ) from None


__all__ = [
    "GatewayCapabilityResumeClient",
    "GatewayCapabilityResumeClientError",
]
