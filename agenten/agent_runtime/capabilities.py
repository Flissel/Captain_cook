"""Deterministic, fail-closed capability grants for runtime commands."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    CapabilityGrant,
    CapabilityGrantRevocation,
    CapabilityProfile,
)
from agenten.validation.contracts import WorkBatch


class CapabilityDenied(RuntimeError):
    """The released work contract does not authorize the requested effect."""


PROFILE_CAPABILITIES: dict[CapabilityProfile, frozenset[str]] = {
    CapabilityProfile.PLANNER: frozenset({"hermes.plan", "minibook.plan"}),
    CapabilityProfile.AGENT_DESIGNER: frozenset(
        {"hermes.plan", "hermes.design_agent", "minibook.plan"}
    ),
    CapabilityProfile.CODE_BUILDER: frozenset(
        {
            "codex.run",
            "codex.resume",
            "codex.status",
            "codex.cancel",
            "codex.heartbeat",
            "workspace.write",
            "tests.run",
        }
    ),
    CapabilityProfile.N8N_BUILDER: frozenset(
        {
            "codex.run",
            "codex.resume",
            "codex.status",
            "codex.cancel",
            "codex.heartbeat",
            "workspace.write",
            "tests.run",
            "mcp.n8n",
        }
    ),
    CapabilityProfile.FACTORY_ARCHITECT: frozenset(
        {
            "hermes.plan",
            "hermes.design_agent",
            "workspace.read",
            "context7.query",
        }
    ),
    CapabilityProfile.FACTORY_TOOL_INTEGRATOR: frozenset(
        {
            "codex.run",
            "codex.resume",
            "codex.status",
            "codex.cancel",
            "codex.heartbeat",
            "workspace.read",
            "workspace.write",
            "tests.run",
            "context7.query",
        }
    ),
    CapabilityProfile.FACTORY_REAL_CASE_TESTER: frozenset(
        {
            "workspace.read",
            "tests.run",
            "evidence.write",
        }
    ),
    CapabilityProfile.FACTORY_QUALITY_WARDEN: frozenset(
        {
            "workspace.read",
            "evidence.read",
            "context7.query",
        }
    ),
}

LEASE_DURATION = timedelta(minutes=15)


def derive_grant(
    command: AgentRuntimeCommand,
    released_batch: WorkBatch,
    now: datetime,
) -> CapabilityGrant:
    """Derive the only grant allowed by an exact command/batch pairing."""

    issued_at = _require_utc(now)
    payload = command.payload
    if payload.batch_id is None or payload.subtask_id is None or payload.workspace_ref is None:
        raise CapabilityDenied("runtime grants require batch, subtask, and workspace bindings")
    if payload.batch_id != released_batch.batch_id:
        raise CapabilityDenied("command batch does not match the released batch")
    if payload.subtask_id not in released_batch.subtask_ids:
        raise CapabilityDenied("command subtask is not part of the released batch")

    profile = payload.capability_profile
    if profile.value not in released_batch.capability_tags:
        raise CapabilityDenied(f"{profile.value} was not released for this batch")
    capabilities = PROFILE_CAPABILITIES[profile]
    if payload.operation.value not in capabilities:
        raise CapabilityDenied(
            f"{payload.operation.value} is not permitted by profile {profile.value}"
        )

    return CapabilityGrant(
        schema_name="captain.capability-grant.v1",
        grant_id=_grant_id(command, released_batch),
        command_id=command.event_id,
        batch_id=released_batch.batch_id,
        batch_version=command.subject_version,
        subtask_id=payload.subtask_id,
        workspace_ref=payload.workspace_ref,
        profile=profile,
        capabilities=tuple(sorted(capabilities)),
        mcp_servers=("n8n-mcp",) if profile is CapabilityProfile.N8N_BUILDER else (),
        issued_at=issued_at,
        expires_at=issued_at + LEASE_DURATION,
    )


def validate_grant(
    grant: CapabilityGrant,
    command: AgentRuntimeCommand,
    now: datetime,
    revocation: CapabilityGrantRevocation | None = None,
) -> CapabilityGrant:
    """Validate an existing grant against its command and current UTC time."""

    checked_at = _require_utc(now)
    payload = command.payload
    if grant.command_id != command.event_id:
        raise CapabilityDenied("grant belongs to a different command")
    if grant.batch_id != payload.batch_id:
        raise CapabilityDenied("grant belongs to a different batch")
    if grant.batch_version != command.subject_version:
        raise CapabilityDenied("grant belongs to a different batch version")
    if grant.subtask_id != payload.subtask_id:
        raise CapabilityDenied("grant belongs to a different subtask")
    if grant.workspace_ref != payload.workspace_ref:
        raise CapabilityDenied("grant belongs to a different workspace")
    if grant.profile is not payload.capability_profile:
        raise CapabilityDenied("grant profile does not match the command profile")
    if grant.issued_at > checked_at:
        raise CapabilityDenied("grant is not active yet")
    if grant.expires_at <= checked_at:
        raise CapabilityDenied("grant has expired")

    expected_capabilities = PROFILE_CAPABILITIES[grant.profile]
    if frozenset(grant.capabilities) != expected_capabilities:
        raise CapabilityDenied("grant capabilities do not exactly match its profile")
    if payload.operation.value not in expected_capabilities:
        raise CapabilityDenied("grant does not authorize the requested operation")
    expected_servers = (
        ("n8n-mcp",) if grant.profile is CapabilityProfile.N8N_BUILDER else ()
    )
    if grant.mcp_servers != expected_servers:
        raise CapabilityDenied("grant MCP servers do not exactly match its profile")
    if revocation is not None:
        if revocation.grant_id != grant.grant_id:
            raise CapabilityDenied("grant revocation belongs to a different grant")
        if revocation.command_id != command.event_id:
            raise CapabilityDenied("grant revocation belongs to a different command")
        raise CapabilityDenied("grant has been revoked by Captain")
    return grant


def _grant_id(command: AgentRuntimeCommand, released_batch: WorkBatch) -> str:
    binding = "|".join(
        (
            str(command.event_id),
            released_batch.batch_id,
            str(command.subject_version),
            command.payload.subtask_id or "",
            command.payload.workspace_ref or "",
            command.payload.capability_profile.value,
        )
    )
    digest = hashlib.sha256(binding.encode("utf-8")).hexdigest()
    return f"grant-{digest[:32]}"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CapabilityDenied("capability policy requires an aware UTC clock")
    if value.utcoffset() != timezone.utc.utcoffset(value):
        raise CapabilityDenied("capability policy requires UTC timestamps")
    return value.astimezone(timezone.utc)
