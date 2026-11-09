"""Agent lifecycle records in the gateway ledger (plan 2026-11-07, Phase 2).

Authentication stays the per-agent bearer token digest in
`WORKER_AGENT_TOKEN_SHA256_JSON`. The ledger adds the lifecycle: which
version of an agent Captain registered, by whom, from which spec, and
whether it was retired. Retirement is terminal; agent ids are never reused.

Capability grants bound what a ledger-registered agent may claim: one
active (unexpired, unrevoked) grant must hold every `capability_tags`
entry of the batch. Grants are not unioned, so each stays a coherent scope.

All agent block types are top-level (`parent_index` None), gateway-owned
and written only through Captain routes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any, Literal, TypeAlias

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from agenten.agent_runtime.contracts import AGENT_ID_PATTERN
from gateway.contracts import BatchProjection

AGENT_REGISTERED_BLOCK_TYPE = "agent_registered"
AGENT_RETIRED_BLOCK_TYPE = "agent_retired"
AGENT_CAPABILITY_GRANTED_BLOCK_TYPE = "agent_capability_granted"
AGENT_CAPABILITY_REVOKED_BLOCK_TYPE = "agent_capability_revoked"
AGENT_LIFECYCLE_BLOCK_TYPES = frozenset(
    {
        AGENT_REGISTERED_BLOCK_TYPE,
        AGENT_RETIRED_BLOCK_TYPE,
        AGENT_CAPABILITY_GRANTED_BLOCK_TYPE,
        AGENT_CAPABILITY_REVOKED_BLOCK_TYPE,
    }
)

RetireReason: TypeAlias = Literal["captain_retired", "operator_retired", "policy_violation"]
RevokeReason: TypeAlias = Literal["captain_revoked", "operator_revoked", "policy_violation"]
GRANT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"
AgentStatus: TypeAlias = Literal["registered", "retired"]


class AgentRegisteredEvent(BaseModel):
    """Captain registers (or re-versions) an agent that holds a token."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    version: str = Field(min_length=1, max_length=64)
    owner: str = Field(min_length=1, max_length=128)
    spec_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class AgentRetireRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: RetireReason


class AgentRetiredEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    reason: RetireReason


class AgentCapabilityGrantRequest(BaseModel):
    """Captain grants an agent a bundle of batch capability tags until expiry."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str = Field(pattern=GRANT_ID_PATTERN)
    capability_tags: tuple[str, ...] = Field(min_length=1, max_length=64)
    expires_at: AwareDatetime

    @field_validator("capability_tags")
    @classmethod
    def _distinct_tags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not tag.strip() or len(tag) > 128 for tag in value):
            raise ValueError("capability tags must be non-empty and at most 128 characters")
        if len(set(value)) != len(value):
            raise ValueError("capability tags must be distinct")
        return tuple(sorted(value))


class AgentCapabilityGrantedEvent(AgentCapabilityGrantRequest):
    agent_id: str = Field(pattern=AGENT_ID_PATTERN)


class AgentCapabilityRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: RevokeReason


class AgentCapabilityRevokedEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str = Field(pattern=AGENT_ID_PATTERN)
    grant_id: str = Field(pattern=GRANT_ID_PATTERN)
    reason: RevokeReason


class AgentGrantProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    grant_id: str
    capability_tags: tuple[str, ...]
    expires_at: AwareDatetime
    revoked: bool = False

    def active(self, now: datetime) -> bool:
        return not self.revoked and self.expires_at > now


class AgentProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    status: AgentStatus
    version: str
    owner: str
    spec_sha256: str
    registration_count: int = Field(ge=1)
    retire_reason: RetireReason | None = None
    grants: tuple[AgentGrantProjection, ...] = ()

    def covers(self, capability_tags: Iterable[str], *, now: datetime) -> bool:
        """True if one active grant holds every tag (grants are not unioned)."""

        if self.status != "registered":
            return False
        required = set(capability_tags)
        return any(
            grant.active(now) and required <= set(grant.capability_tags) for grant in self.grants
        )


class AgentLedgerError(ValueError):
    """The stored agent records violate the lifecycle rules."""


def project_agent(blocks: Sequence[dict[str, Any]], agent_id: str) -> AgentProjection | None:
    """Derive one agent's lifecycle from its blocks; None if never registered."""

    records = sorted(
        (
            block
            for block in blocks
            if block.get("block_type") in AGENT_LIFECYCLE_BLOCK_TYPES
            and isinstance(block.get("data"), dict)
            and block["data"].get("agent_id") == agent_id
        ),
        key=lambda block: int(block["index"]),
    )
    current: AgentRegisteredEvent | None = None
    versions: set[str] = set()
    count = 0
    retired: AgentRetiredEvent | None = None
    grants: dict[str, AgentGrantProjection] = {}
    for block in records:
        block_type = block["block_type"]
        if retired is not None:
            raise AgentLedgerError("agent record after agent_retired")
        if block_type == AGENT_REGISTERED_BLOCK_TYPE:
            event = AgentRegisteredEvent.model_validate(block["data"])
            if event.version in versions:
                raise AgentLedgerError("agent version registered twice")
            versions.add(event.version)
            current = event
            count += 1
            continue
        if current is None:
            raise AgentLedgerError(f"{block_type} before agent_registered")
        if block_type == AGENT_CAPABILITY_GRANTED_BLOCK_TYPE:
            grant = AgentCapabilityGrantedEvent.model_validate(block["data"])
            if grant.grant_id in grants:
                raise AgentLedgerError("capability grant granted twice")
            grants[grant.grant_id] = AgentGrantProjection(
                grant_id=grant.grant_id,
                capability_tags=grant.capability_tags,
                expires_at=grant.expires_at,
            )
            continue
        if block_type == AGENT_CAPABILITY_REVOKED_BLOCK_TYPE:
            revocation = AgentCapabilityRevokedEvent.model_validate(block["data"])
            existing = grants.get(revocation.grant_id)
            if existing is None:
                raise AgentLedgerError("revocation of an unknown grant")
            if existing.revoked:
                raise AgentLedgerError("capability grant revoked twice")
            grants[revocation.grant_id] = existing.model_copy(update={"revoked": True})
            continue
        retired = AgentRetiredEvent.model_validate(block["data"])
    if current is None:
        return None
    return AgentProjection(
        agent_id=agent_id,
        status="retired" if retired is not None else "registered",
        version=current.version,
        owner=current.owner,
        spec_sha256=current.spec_sha256,
        registration_count=count,
        retire_reason=retired.reason if retired is not None else None,
        grants=tuple(grants.values()),
    )


# --- derived claim outcomes ---------------------------------------------------
#
# What an agent achieved is read from the batch projections, not written a
# second time: each `batch_claimed` names the agent and its fencing token,
# and the batch's own lifecycle says how that claim ended.

ClaimOutcome: TypeAlias = Literal[
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "failed_after_max_iterations",
    "aborted_infra",
    "superseded",
    "released",
    "active",
]
_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "rejected", "cancelled", "failed_after_max_iterations", "aborted_infra"}
)


class AgentClaimOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    batch_id: str
    fencing_token: int = Field(ge=1)
    outcome: ClaimOutcome


def claim_outcome(projection: BatchProjection, fencing_token: int) -> ClaimOutcome:
    """How the claim with `fencing_token` on this batch ended (or not yet)."""

    if fencing_token < projection.claim_iteration:
        return "superseded"
    if projection.status in _TERMINAL_STATUSES:
        return projection.status  # type: ignore[return-value]
    if projection.recovered_iteration == fencing_token:
        return "released"
    return "active"
