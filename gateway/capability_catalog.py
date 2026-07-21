"""Read-only Package-A adapter over Captain's authoritative catalog head."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_factory.capability_resolution import CapabilityCatalogPort
from agenten.agent_factory.contracts import AgentFactoryJobV2, PromotedCapability
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent
from gateway.contracts import CapabilityReleaseRequest


class CapabilityCompatibilityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str = Field(min_length=1)
    minimum_version: int = Field(ge=1, strict=True)
    schema_major: int = Field(ge=1, strict=True)
    accepted_assertion_ids: tuple[str, ...] = Field(min_length=1)
    integration_intents: tuple[IntegrationIntent, ...] = ()
    tool_contracts: tuple[str, ...] = ()


class CapabilityCatalogRecord(BaseModel):
    """Public catalog head; package bytes and private evidence are deliberately absent."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )

    schema_name: Literal["captain.capability-catalog-record.v1"] = Field(
        default="captain.capability-catalog-record.v1",
        alias="schema",
        serialization_alias="schema",
    )
    capability_id: str = Field(min_length=1)
    capability_version: int = Field(ge=1, strict=True)
    team_version: int = Field(ge=1, strict=True)
    schema_major: int = Field(ge=1, strict=True)
    package_ref: ArtifactRef
    release_authority_job_id: UUID
    terminal_decision_id: UUID
    promoted_capability: PromotedCapability
    accepted_assertion_ids: tuple[str, ...] = Field(min_length=1)
    integration_intents: tuple[IntegrationIntent, ...] = ()
    tool_contracts: tuple[str, ...] = ()
    unresolved_required_gap_ids: tuple[str, ...] = ()
    status: Literal["ready_to_use", "revoked"]
    catalog_fence: int = Field(ge=1, strict=True)
    published_at: datetime

    @classmethod
    def from_release(
        cls,
        release: CapabilityReleaseRequest,
        *,
        catalog_fence: int,
    ) -> "CapabilityCatalogRecord":
        return cls(
            capability_id=release.package.capability_id,
            capability_version=release.package.capability_version,
            team_version=release.team_version,
            schema_major=release.schema_major,
            package_ref=release.package_ref,
            release_authority_job_id=release.package.factory_job_id,
            terminal_decision_id=release.decision.decision_id,
            promoted_capability=release.promoted_capability,
            accepted_assertion_ids=release.accepted_assertion_ids,
            integration_intents=release.integration_intents,
            tool_contracts=release.tool_contracts,
            status="ready_to_use",
            catalog_fence=catalog_fence,
            published_at=release.occurred_at,
        )

    def satisfies(self, request: CapabilityCompatibilityRequest) -> bool:
        return (
            self.status == "ready_to_use"
            and not self.unresolved_required_gap_ids
            and self.capability_id == request.capability_id
            and self.capability_version >= request.minimum_version
            and self.schema_major == request.schema_major
            and self.accepted_assertion_ids == request.accepted_assertion_ids
            and self.integration_intents == request.integration_intents
            and self.tool_contracts == request.tool_contracts
            and self.promoted_capability.status == "ready_to_use"
        )


class CapabilityCatalogRepository(Protocol):
    def find_ready_capability(
        self,
        capability_id: str,
    ) -> CapabilityCatalogRecord | None: ...


class GatewayCapabilityCatalog(CapabilityCatalogPort):
    """Reuse only a public, exact, non-revoked Gateway catalog record."""

    def __init__(self, repository: CapabilityCatalogRepository) -> None:
        self._repository = repository

    def compatible_capability(
        self,
        job: AgentFactoryJobV2,
    ) -> PromotedCapability | None:
        record = self.compatible_record(job)
        return record.promoted_capability if record is not None else None

    def compatible_record(
        self,
        job: AgentFactoryJobV2,
    ) -> CapabilityCatalogRecord | None:
        """Return the complete frozen authority needed by an execution replay."""

        record = self._repository.find_ready_capability(job.required_capability)
        if record is None:
            return None
        try:
            request = compatibility_request_for_authority(job, record)
        except ValueError:
            return None
        if not record.satisfies(request):
            return None
        promoted = record.promoted_capability
        if (
            promoted.capability_id != record.capability_id
            or promoted.version != record.capability_version
            or promoted.status != "ready_to_use"
        ):
            return None
        return record


def compatibility_request_for_authority(
    job: AgentFactoryJobV2,
    record: CapabilityCatalogRecord,
) -> CapabilityCompatibilityRequest:
    """Derive the exact public request while rejecting incoherent tool authority."""

    intents = record.integration_intents
    contracts = record.tool_contracts
    if IntegrationIntent.NONE in intents or len(intents) != len(set(intents)):
        raise ValueError("catalog integration intents are not canonical")
    if tuple(sorted(intents, key=lambda item: item.value)) != intents:
        raise ValueError("catalog integration intents are not canonical")
    if len(contracts) != len(set(contracts)) or tuple(sorted(contracts)) != contracts:
        raise ValueError("catalog tool contracts are not canonical")

    n8n_contracts = []
    for contract in contracts:
        if "\\" in contract:
            raise ValueError("catalog tool contract path is not canonical")
        path = PurePosixPath(contract)
        if path.is_absolute() or "." in path.parts or ".." in path.parts:
            raise ValueError("catalog tool contract path is not canonical")
        if contract.startswith("n8n/") and contract.endswith(".json"):
            n8n_contracts.append(contract)
        elif not contract.startswith("adapters/"):
            raise ValueError("catalog tool contract type is unsupported")

    declares_n8n = IntegrationIntent.N8N in intents
    if declares_n8n != bool(n8n_contracts):
        raise ValueError("catalog n8n intent and workflow contracts disagree")
    if len(record.promoted_capability.tool_refs) != len(contracts):
        raise ValueError("catalog tool contracts do not match promoted tool references")
    if len(record.promoted_capability.tool_refs) != len(
        set(record.promoted_capability.tool_refs)
    ):
        raise ValueError("catalog promoted tool references are not unique")

    return CapabilityCompatibilityRequest(
        capability_id=job.required_capability,
        minimum_version=job.subject_version,
        schema_major=1,
        accepted_assertion_ids=job.acceptance_assertion_ids,
        integration_intents=intents,
        tool_contracts=contracts,
    )
