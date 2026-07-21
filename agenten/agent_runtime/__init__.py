"""Typed cross-runtime control-plane contracts and orchestration."""

from typing import TYPE_CHECKING

from .contracts import (
    AgentBlueprint,
    AgentRuntimeCommand,
    AgentRuntimeResult,
    ArtifactRef,
    CapabilityGrant,
    CapabilityGrantRevocation,
    CapabilityProfile,
    HermesPlanResult,
    IntegrationIntent,
    RuntimeOperation,
    RuntimeStatus,
)
from .capabilities import CapabilityDenied, derive_grant, validate_grant
from .prompt_policy import PromptPolicyDenied, RenderedPrompt, render_codex_prompt
from .service import AgentRuntimeService, RuntimeContractViolation
from .gateway_client import GatewayRuntimeClient
from .http_server import RuntimeCommandExecutor, create_runtime_app
from .runtime_entrypoint import (
    GatewayBackedRuntimeState,
    RuntimeConfigurationError,
    RuntimeEntrypointSettings,
    compose_gateway_backed_runtime_app,
)
from .tools import RuntimeToolContext, RuntimeToolset, available_tools
from .swarm import RuntimeTaskProjection, SwarmOrchestrator

if TYPE_CHECKING:
    from .control_plane import (
        AgentRuntimeControlPlane,
        ControlPlaneEvidenceManifest,
        ControlPlaneRunRequest,
        ControlPlaneRunResult,
        InMemoryControlPlaneRunStore,
        JsonControlPlaneRunStore,
        ValidationDisposition,
        ValidationRecord,
    )


_CONTROL_PLANE_EXPORTS = frozenset(
    {
        "AgentRuntimeControlPlane",
        "ControlPlaneEvidenceManifest",
        "ControlPlaneRunRequest",
        "ControlPlaneRunResult",
        "InMemoryControlPlaneRunStore",
        "JsonControlPlaneRunStore",
        "ValidationDisposition",
        "ValidationRecord",
    }
)


def __getattr__(name: str) -> object:
    if name in _CONTROL_PLANE_EXPORTS:
        from . import control_plane

        return getattr(control_plane, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = [
    "AgentBlueprint",
    "AgentRuntimeCommand",
    "AgentRuntimeResult",
    "ArtifactRef",
    "CapabilityGrant",
    "CapabilityGrantRevocation",
    "CapabilityProfile",
    "HermesPlanResult",
    "IntegrationIntent",
    "RuntimeOperation",
    "RuntimeStatus",
    "CapabilityDenied",
    "derive_grant",
    "validate_grant",
    "PromptPolicyDenied",
    "RenderedPrompt",
    "render_codex_prompt",
    "AgentRuntimeService",
    "RuntimeContractViolation",
    "GatewayRuntimeClient",
    "RuntimeCommandExecutor",
    "create_runtime_app",
    "GatewayBackedRuntimeState",
    "RuntimeConfigurationError",
    "RuntimeEntrypointSettings",
    "compose_gateway_backed_runtime_app",
    "RuntimeToolContext",
    "RuntimeToolset",
    "available_tools",
    "RuntimeTaskProjection",
    "SwarmOrchestrator",
    "AgentRuntimeControlPlane",
    "ControlPlaneEvidenceManifest",
    "ControlPlaneRunRequest",
    "ControlPlaneRunResult",
    "InMemoryControlPlaneRunStore",
    "JsonControlPlaneRunStore",
    "ValidationDisposition",
    "ValidationRecord",
]
