"""Concrete bridge from capability-live restart to authenticated Gateway ports.

The bridge owns no resume identity: Gateway responses are passed through after
their existing typed contracts validate them.  A deployment binds its current
authenticated Gateway/runtime client to ``CapabilityResumeGatewayPort``.
"""

from __future__ import annotations

from typing import Protocol

from gateway.capability_live_pipeline import (
    CapabilityExecutionAuthority,
    CapabilityResumeAuthorization,
    CapabilityResumeExecutionRequest,
)


class CapabilityResumeGatewayPort(Protocol):
    """Authenticated Gateway boundary for one existing resume authority."""

    async def authorize_resume(
        self,
        request: CapabilityResumeExecutionRequest,
    ) -> CapabilityResumeAuthorization | None: ...

    async def dispatch_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority | None: ...

    async def readback_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority | None: ...


class GatewayCapabilityResumeExecutionAdapter:
    """Concrete capability-live port that returns Gateway-observed authorities."""

    def __init__(self, *, gateway: CapabilityResumeGatewayPort) -> None:
        self._gateway = gateway

    async def authorize_resume(
        self,
        request: CapabilityResumeExecutionRequest,
    ) -> CapabilityResumeAuthorization | None:
        return await self._gateway.authorize_resume(request)

    async def dispatch_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority | None:
        return await self._gateway.dispatch_resume(authorization)

    async def readback_resume(
        self,
        authorization: CapabilityResumeAuthorization,
    ) -> CapabilityExecutionAuthority | None:
        return await self._gateway.readback_resume(authorization)


__all__ = [
    "CapabilityResumeGatewayPort",
    "GatewayCapabilityResumeExecutionAdapter",
]
