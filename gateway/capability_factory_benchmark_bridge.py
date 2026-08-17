"""Canonical bridge from Factory V3 quality dispatch to the generic benchmark."""

from __future__ import annotations

from agenten.agent_factory.orchestration import FactoryDispatchError


class AssemblyOwnedGenericBenchmark:
    """Fail closed until the assembly-owned quality materializer is bound."""

    async def dispatch(self, request):
        del request
        raise FactoryDispatchError(
            "generic benchmark dispatch is owned by the capability assembly"
        )
