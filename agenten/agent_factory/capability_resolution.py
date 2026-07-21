"""Transport-neutral capability reuse decision before Forge submission."""

from __future__ import annotations

import hashlib
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agenten.agent_factory.contracts import AgentFactoryJobV2, PromotedCapability


class CapabilityCatalogPort(Protocol):
    def compatible_capability(self, job: AgentFactoryJobV2) -> PromotedCapability | None: ...


class CapabilityResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_name: Literal["captain.capability-resolution.v1"] = "captain.capability-resolution.v1"
    kind: Literal["reuse", "create"]
    capability: PromotedCapability | None = None
    creation_key: str | None = Field(default=None, pattern=r"^factory-create-[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_exact_outcome_payload(self) -> "CapabilityResolution":
        if self.kind == "reuse" and (self.capability is None or self.creation_key is not None):
            raise ValueError("reuse requires only a promoted capability")
        if self.kind == "create" and (self.capability is not None or self.creation_key is None):
            raise ValueError("create requires only a creation key")
        return self


class CapabilityResolver:
    def __init__(self, catalog: CapabilityCatalogPort) -> None:
        self._catalog = catalog

    def resolve(self, job: AgentFactoryJobV2) -> CapabilityResolution:
        capability = self._catalog.compatible_capability(job)
        if capability is not None and _matches_job(capability, job):
            return CapabilityResolution(kind="reuse", capability=capability)
        return CapabilityResolution(kind="create", creation_key=_creation_key(job))


def _matches_job(capability: PromotedCapability, job: AgentFactoryJobV2) -> bool:
    return (
        capability.capability_id == job.required_capability
        and capability.version >= job.subject_version
        and capability.status == "ready_to_use"
        and capability.promotion_block_ref is not None
    )


def _creation_key(job: AgentFactoryJobV2) -> str:
    material = "\x1f".join(
        (
            job.required_capability,
            str(job.subject_version),
            job.input_ref.sha256,
            job.compiled_spec_ref.sha256,
            job.dependency_graph_ref.sha256,
            *job.acceptance_assertion_ids,
        )
    )
    return "factory-create-" + hashlib.sha256(material.encode()).hexdigest()
