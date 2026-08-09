"""Concrete assembly point for the current-authority capability-live graph.

The builder deliberately accepts already configured Gateway-owned ports.  It
performs no provider, n8n, Minibook, Docker, or database effect while wiring
them, which keeps the same production entrypoint executable with typed offline
fakes.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType

from agenten.agent_factory.capability_live_runner import CapabilityLiveRunner
from gateway.agent_factory_live_composition import (
    AgentFactoryLiveComposition,
    GatewayCapabilityLiveCompositionError,
    compose_gateway_capability_live_runner,
)
from gateway.capability_live_factory_adapter import CapabilityFactoryProvisioningPort
from gateway.capability_live_current_adapters import (
    CapabilityProjectionEffectsPort,
)
from gateway.capability_live_runtime_resume import (
    GatewayCapabilityResumeExecutionAdapter,
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
    resume_execution: GatewayCapabilityResumeExecutionAdapter


@dataclass(frozen=True)
class CapabilityLiveAuthorityAdapterContext:
    """Verified local context supplied to the configured authority factory."""

    repository_root: Path
    settings: "CapabilityLiveProductionSettings"


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


def build_current_authority_capability_live_runner_from_environment(
    *,
    repository_root: Path,
    environment: Mapping[str, str],
    clock: Callable[[], datetime] | None = None,
) -> CapabilityLiveRunner:
    """Load a digest-bound local authority factory before composing the runner.

    The configured module supplies real Factory, projection, and runtime ports;
    this assembly validates local configuration and module bytes only.  It does
    not contact Gateway, Minibook, providers, or n8n while bootstrapping.
    """

    root = repository_root.resolve(strict=True)
    settings = CapabilityLiveProductionSettings.from_environment(environment)
    relative_path = environment.get("CAPTAIN_CAPABILITY_LIVE_ADAPTER", "").strip()
    expected_digest = environment.get(
        "CAPTAIN_CAPABILITY_LIVE_ADAPTER_SHA256", ""
    ).strip()
    if not relative_path or not _is_sha256(expected_digest):
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter is blocked"
        )
    adapter_path = (root / relative_path).resolve(strict=True)
    if not adapter_path.is_relative_to(root) or not adapter_path.is_file():
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter is unavailable"
        )
    try:
        source = adapter_path.read_bytes()
    except OSError as exc:
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter is unavailable"
        ) from exc
    if not hmac.compare_digest(hashlib.sha256(source).hexdigest(), expected_digest):
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter digest changed"
        )
    module = ModuleType(
        f"_captain_capability_authorities_{expected_digest[:16]}"
    )
    module.__file__ = str(adapter_path)
    try:
        exec(compile(source, str(adapter_path), "exec", dont_inherit=True), module.__dict__)
    except Exception as exc:
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter could not load"
        ) from exc
    factory = getattr(module, "build_capability_live_authorities", None)
    if not callable(factory):
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter factory is unavailable"
        )
    try:
        authorities = factory(
            CapabilityLiveAuthorityAdapterContext(
                repository_root=root,
                settings=settings,
            )
        )
    except Exception as exc:
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter could not compose"
        ) from exc
    if not isinstance(authorities, CapabilityLiveCurrentAuthorities):
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter returned invalid authorities"
        )
    return build_current_authority_capability_live_runner(
        repository_root=root,
        authorities=authorities,
        clock=clock,
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


__all__ = [
    "CapabilityLiveAuthorityAdapterContext",
    "CapabilityLiveCurrentAuthorities",
    "CapabilityLiveProductionConfigurationError",
    "CapabilityLiveProductionSettings",
    "build_current_authority_capability_live_runner",
    "build_current_authority_capability_live_runner_from_environment",
]
