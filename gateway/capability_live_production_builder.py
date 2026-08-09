"""Concrete assembly point for the current-authority capability-live graph.

The builder deliberately accepts already configured Gateway-owned ports.  It
performs no provider, n8n, Minibook, Docker, or database effect while wiring
them, which keeps the same production entrypoint executable with typed offline
fakes.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agenten.agent_factory.capability_live_runner import CapabilityLiveRunner
from agenten.agent_runtime.production_bootstrap import (
    RuntimeAdapterManifestError,
    load_verified_repository_module,
)
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
    settings: "CapabilityLiveProductionSettings" = field(repr=False)


@dataclass(frozen=True)
class CapabilityLiveProductionSettings:
    """Redacted configuration aliases required before provider-bearing ports."""

    gateway_url: str
    gateway_token: str = field(repr=False)
    benchmark_provider: str
    benchmark_model: str
    minibook_backend_url: str
    minibook_projection_api_key: str = field(repr=False)

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
    try:
        module = load_verified_repository_module(
            relative_path,
            expected_sha256=expected_digest,
            repository_root=root,
        )
    except RuntimeAdapterManifestError as exc:
        raise CapabilityLiveProductionConfigurationError(
            f"current capability live authority adapter {exc}"
        ) from None
    factory = getattr(module, "build_capability_live_authorities", None)
    _require_method_contract(
        factory,
        qualified_name="factory build_capability_live_authorities",
        argument_count=1,
        asynchronous=False,
    )
    try:
        authorities = factory(
            CapabilityLiveAuthorityAdapterContext(
                repository_root=root,
                settings=settings,
            )
        )
    except Exception:
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter could not compose"
        ) from None
    if not isinstance(authorities, CapabilityLiveCurrentAuthorities):
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority adapter returned invalid authorities"
        )
    _validate_authority_bindings(authorities)
    return build_current_authority_capability_live_runner(
        repository_root=root,
        authorities=authorities,
        clock=clock,
    )


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _require_method_contract(
    method: object,
    *,
    qualified_name: str,
    argument_count: int,
    asynchronous: bool,
) -> None:
    if not callable(method):
        raise CapabilityLiveProductionConfigurationError(
            f"current capability live authority {qualified_name} must be callable"
        )
    if inspect.iscoroutinefunction(method) is not asynchronous:
        requirement = "async" if asynchronous else "synchronous"
        raise CapabilityLiveProductionConfigurationError(
            f"current capability live authority {qualified_name} must be {requirement}"
        )
    arguments = [object()] * argument_count
    try:
        signature = inspect.signature(method)
        signature.bind(*arguments)
    except (TypeError, ValueError):
        raise CapabilityLiveProductionConfigurationError(
            f"current capability live authority {qualified_name} has an invalid signature"
        ) from None
    for invalid_arguments in (arguments[:-1], [*arguments, object()]):
        try:
            signature.bind(*invalid_arguments)
        except TypeError:
            continue
        raise CapabilityLiveProductionConfigurationError(
            f"current capability live authority {qualified_name} has an invalid signature"
        )


def _validate_authority_bindings(
    authorities: CapabilityLiveCurrentAuthorities,
) -> None:
    if not isinstance(authorities.factory, AgentFactoryLiveComposition):
        raise CapabilityLiveProductionConfigurationError(
            "current capability live authority factory binding is invalid"
        )
    method_contracts = (
        (authorities.factory, "factory.run", 1, True),
        (authorities.factory, "factory.projection", 1, False),
        (authorities.factory, "factory.snapshot", 1, False),
        (authorities.factory, "factory.workflow_artifacts", 1, False),
        (authorities.factory, "factory.workflow_budget_projection", 1, False),
        (
            authorities.factory,
            "factory.business_benchmark_summary_by_artifact",
            1,
            False,
        ),
        (authorities.provisioning, "provisioning.provision", 1, False),
        (authorities.provisioning, "provisioning.binding_for", 1, False),
        (authorities.minibook_effects, "minibook_effects.project_and_ack", 1, True),
        (authorities.minibook_effects, "minibook_effects.readback", 1, True),
        (authorities.resume_execution, "resume_execution.authorize_resume", 1, True),
        (authorities.resume_execution, "resume_execution.dispatch_resume", 1, True),
        (authorities.resume_execution, "resume_execution.readback_resume", 1, True),
    )
    for binding, qualified_name, argument_count, asynchronous in method_contracts:
        try:
            method = getattr(binding, qualified_name.rsplit(".", 1)[1])
        except Exception:
            method = None
        _require_method_contract(
            method,
            qualified_name=qualified_name,
            argument_count=argument_count,
            asynchronous=asynchronous,
        )


__all__ = [
    "CapabilityLiveAuthorityAdapterContext",
    "CapabilityLiveCurrentAuthorities",
    "CapabilityLiveProductionConfigurationError",
    "CapabilityLiveProductionSettings",
    "build_current_authority_capability_live_runner",
    "build_current_authority_capability_live_runner_from_environment",
]
