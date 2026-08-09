"""Concrete assembly point for the current-authority capability-live graph.

The builder deliberately accepts already configured Gateway-owned ports.  It
performs no provider, n8n, Minibook, Docker, or database effect while wiring
them, which keeps the same production entrypoint executable with typed offline
fakes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agenten.agent_factory.capability_live_runner import CapabilityLiveRunner
from gateway.agent_factory_live_composition import (
    AgentFactoryLiveComposition,
    GatewayCapabilityLiveCompositionError,
    compose_gateway_capability_live_runner,
)
from gateway.capability_live_factory_adapter import CapabilityFactoryProvisioningPort
from gateway.capability_live_current_adapters import (
    CapabilityProjectionEffectsPort,
    CapabilityResumeExecutionPort,
)


class CapabilityLiveProductionConfigurationError(
    GatewayCapabilityLiveCompositionError
):
    """A required local production setting is absent before any external cost."""


@dataclass(frozen=True)
class CapabilityLiveCurrentAuthorities:
    """The four real seams that the seven-stage runner is allowed to join."""

    factory: AgentFactoryLiveComposition
    provisioning: CapabilityFactoryProvisioningPort
    minibook_effects: CapabilityProjectionEffectsPort
    resume_execution: CapabilityResumeExecutionPort


@dataclass(frozen=True)
class CapabilityLiveProductionSettings:
    """Redacted configuration aliases required before provider-bearing ports."""

    gateway_url: str
    gateway_token: str
    benchmark_provider: str
    benchmark_model: str
    minibook_backend_url: str
    minibook_projection_api_key: str

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str]
    ) -> "CapabilityLiveProductionSettings":
        values = {
            "gateway_url": environment.get("CAPTAIN_GATEWAY_URL", "").strip(),
            "gateway_token": environment.get("CAPTAIN_GATEWAY_TOKEN", "").strip(),
            "benchmark_provider": environment.get("CAPTAIN_BENCHMARK_PROVIDER", "").strip(),
            "benchmark_model": environment.get("CAPTAIN_BENCHMARK_MODEL", "").strip(),
            "minibook_backend_url": environment.get(
                "MINIBOOK_BACKEND_URL", ""
            ).strip(),
            "minibook_projection_api_key": environment.get(
                "MINIBOOK_PROJECTION_API_KEY", ""
            ).strip(),
        }
        missing = tuple(key for key, value in values.items() if not value)
        if missing:
            raise CapabilityLiveProductionConfigurationError(
                "current capability live configuration is blocked"
            )
        if values["benchmark_provider"] != "openai":
            raise CapabilityLiveProductionConfigurationError(
                "current capability benchmark provider is unsupported"
            )
        if not values["gateway_url"].startswith(("http://", "https://")):
            raise CapabilityLiveProductionConfigurationError(
                "current capability Gateway URL is invalid"
            )
        if not values["minibook_backend_url"].startswith(("http://", "https://")):
            raise CapabilityLiveProductionConfigurationError(
                "current capability Minibook URL is invalid"
            )
        return cls(**values)


def build_current_authority_capability_live_runner(
    *,
    repository_root: Path,
    authorities: CapabilityLiveCurrentAuthorities,
    clock: Callable[[], datetime] | None = None,
) -> CapabilityLiveRunner:
    """Build the real seven-stage graph from current Gateway authority ports."""

    return compose_gateway_capability_live_runner(
        repository_root=repository_root,
        factory_composition=authorities.factory,
        factory_provisioning=authorities.provisioning,
        minibook_effects=authorities.minibook_effects,
        resume_execution=authorities.resume_execution,
        clock=clock,
    )


__all__ = [
    "CapabilityLiveCurrentAuthorities",
    "CapabilityLiveProductionConfigurationError",
    "CapabilityLiveProductionSettings",
    "build_current_authority_capability_live_runner",
]
