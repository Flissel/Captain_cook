"""Dynamic, Gateway-preflighted provider proxy contexts."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable
from typing import Protocol

from .contracts import ProviderBudgetContextV1
from .service import BudgetRejected, ProviderBudgetProxy


_CONTEXT_REFERENCE = re.compile(r"^gateway://provider-budget-contexts/([0-9a-f]{64})$")


class ProviderBudgetContextResolver(Protocol):
    async def resolve_provider_authority(
        self, reference: str
    ) -> ProviderBudgetContextV1: ...


class DynamicProviderBudgetProxySupervisor:
    """Preflight every request and isolate one proxy state per exact context."""

    def __init__(
        self,
        *,
        resolver: ProviderBudgetContextResolver,
        proxy_factory: Callable[[ProviderBudgetContextV1], ProviderBudgetProxy],
    ) -> None:
        self._resolver = resolver
        self._proxy_factory = proxy_factory
        self._contexts: dict[str, ProviderBudgetContextV1] = {}
        self._proxies: dict[str, ProviderBudgetProxy] = {}
        self._lock = asyncio.Lock()

    async def proxy_for(self, reference: str) -> ProviderBudgetProxy:
        match = _CONTEXT_REFERENCE.fullmatch(reference)
        if match is None:
            raise BudgetRejected("provider authority is unavailable")
        try:
            context = await self._resolver.resolve_provider_authority(reference)
        except Exception:
            raise BudgetRejected("provider authority is unavailable") from None
        digest = match.group(1)
        if context.sha256 != digest:
            raise BudgetRejected("provider authority is unavailable")
        async with self._lock:
            prior = self._contexts.get(digest)
            if prior is not None and prior != context:
                raise BudgetRejected("provider authority is unavailable")
            if prior is None:
                self._contexts[digest] = context
                self._proxies[digest] = self._proxy_factory(context)
            return self._proxies[digest]


__all__ = [
    "DynamicProviderBudgetProxySupervisor",
    "ProviderBudgetContextResolver",
]
