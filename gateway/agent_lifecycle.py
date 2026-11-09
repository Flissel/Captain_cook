"""Agent lifecycle records in the gateway ledger (plan 2026-11-07, Phase 2).

Authentication stays the per-agent bearer token digest in
`WORKER_AGENT_TOKEN_SHA256_JSON`. The ledger adds the lifecycle: which
version of an agent Captain registered, by whom, from which spec, and
whether it was retired. Retirement is terminal; agent ids are never reused.

Both block types are top-level (`parent_index` None), gateway-owned and
written only through Captain routes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from agenten.agent_runtime.contracts import AGENT_ID_PATTERN

AGENT_REGISTERED_BLOCK_TYPE = "agent_registered"
AGENT_RETIRED_BLOCK_TYPE = "agent_retired"
AGENT_LIFECYCLE_BLOCK_TYPES = frozenset({AGENT_REGISTERED_BLOCK_TYPE, AGENT_RETIRED_BLOCK_TYPE})

RetireReason: TypeAlias = Literal["captain_retired", "operator_retired", "policy_violation"]
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


class AgentProjection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    agent_id: str
    status: AgentStatus
    version: str
    owner: str
    spec_sha256: str
    registration_count: int = Field(ge=1)
    retire_reason: RetireReason | None = None


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
    for block in records:
        if retired is not None:
            raise AgentLedgerError("agent record after agent_retired")
        if block["block_type"] == AGENT_REGISTERED_BLOCK_TYPE:
            event = AgentRegisteredEvent.model_validate(block["data"])
            if event.version in versions:
                raise AgentLedgerError("agent version registered twice")
            versions.add(event.version)
            current = event
            count += 1
            continue
        if current is None:
            raise AgentLedgerError("agent_retired before agent_registered")
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
    )
