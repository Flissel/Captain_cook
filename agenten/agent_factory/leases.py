"""Fail-closed issuance and validation of Captain factory-role leases."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Protocol

from agenten.agent_factory.contracts import (
    AgentFactoryJobV3,
    FactoryJob,
    FactoryLease,
    FactoryRole,
)
from agenten.agent_runtime.capabilities import PROFILE_CAPABILITIES
from agenten.agent_runtime.contracts import CapabilityProfile
from agenten.agent_runtime.contracts import IntegrationIntent


FACTORY_LEASE_DURATION = timedelta(minutes=15)

_ROLE_PROFILES: dict[FactoryRole, CapabilityProfile] = {
    FactoryRole.AGENT_ARCHITECT: CapabilityProfile.FACTORY_ARCHITECT,
    FactoryRole.TOOL_INTEGRATOR: CapabilityProfile.FACTORY_TOOL_INTEGRATOR,
    FactoryRole.REAL_CASE_TESTER: CapabilityProfile.FACTORY_REAL_CASE_TESTER,
    FactoryRole.QUALITY_WARDEN: CapabilityProfile.FACTORY_QUALITY_WARDEN,
}


class FactoryLeaseDenied(RuntimeError):
    """The requested Hermes dispatch is outside Captain's active grant."""


class FactoryLeasePort(Protocol):
    def active(
        self, job: FactoryJob, role: FactoryRole, attempt: int, now: datetime
    ) -> FactoryLease:
        """Return an exact, unexpired lease or raise FactoryLeaseDenied."""


def issue_factory_lease(
    *,
    job: FactoryJob,
    role: FactoryRole,
    attempt: int,
    workspace_ref: str,
    now: datetime,
    integration_intent: IntegrationIntent = IntegrationIntent.NONE,
) -> FactoryLease:
    issued_at = _require_utc(now)
    _require_execution_intent(job, integration_intent)
    profile = _ROLE_PROFILES[role]
    if role is FactoryRole.TOOL_INTEGRATOR and integration_intent is IntegrationIntent.N8N:
        profile = CapabilityProfile.N8N_BUILDER
    capabilities = expected_factory_capabilities(job, role, profile)
    lease_id = _factory_lease_id(
        job=job,
        role=role,
        attempt=attempt,
        workspace_ref=workspace_ref,
        integration_intent=integration_intent,
        profile=profile,
        capabilities=capabilities,
    )
    return FactoryLease(
        schema_name="captain.factory-lease.v1",
        lease_id=lease_id,
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        subject_version=job.subject_version,
        attempt=attempt,
        role=role,
        capability_profile=profile,
        integration_intent=integration_intent,
        capabilities=tuple(sorted(capabilities)),
        workspace_ref=workspace_ref,
        issued_at=issued_at,
        expires_at=issued_at + FACTORY_LEASE_DURATION,
    )


def validate_factory_lease(
    lease: FactoryLease,
    *,
    job: FactoryJob,
    role: FactoryRole,
    attempt: int,
    now: datetime,
) -> FactoryLease:
    checked_at = _require_utc(now)
    _require_execution_intent(job, lease.integration_intent)
    if lease.job_id != job.job_id or lease.correlation_id != job.correlation_id:
        raise FactoryLeaseDenied("factory lease belongs to a different job")
    if lease.subject_version != job.subject_version or lease.attempt != attempt:
        raise FactoryLeaseDenied("factory lease has a stale version or attempt")
    if lease.role is not role:
        raise FactoryLeaseDenied("factory lease belongs to a different role")
    if lease.issued_at > checked_at or lease.expires_at <= checked_at:
        raise FactoryLeaseDenied("factory lease is not active")
    expected = expected_factory_capabilities(job, role, lease.capability_profile)
    if frozenset(lease.capabilities) != expected:
        raise FactoryLeaseDenied("factory lease capabilities do not match role profile")
    expected_lease_id = _factory_lease_id(
        job=job,
        role=role,
        attempt=attempt,
        workspace_ref=lease.workspace_ref,
        integration_intent=lease.integration_intent,
        profile=lease.capability_profile,
        capabilities=expected,
    )
    if lease.lease_id != expected_lease_id:
        raise FactoryLeaseDenied("factory lease ID does not match its authority")
    return lease


def expected_factory_capabilities(
    job: FactoryJob,
    role: FactoryRole,
    profile: CapabilityProfile,
) -> frozenset[str]:
    expected = set(PROFILE_CAPABILITIES[profile])
    if isinstance(job, AgentFactoryJobV3) and role is FactoryRole.REAL_CASE_TESTER:
        expected.update(
            capability.value for capability in job.execution_policy.live_capabilities
        )
    return frozenset(expected)


def _require_execution_intent(
    job: FactoryJob, integration_intent: IntegrationIntent
) -> None:
    if (
        isinstance(job, AgentFactoryJobV3)
        and not job.execution_policy.live_execution
        and integration_intent is IntegrationIntent.N8N
    ):
        raise FactoryLeaseDenied("offline factory execution forbids n8n authority")


def _factory_lease_id(
    *,
    job: FactoryJob,
    role: FactoryRole,
    attempt: int,
    workspace_ref: str,
    integration_intent: IntegrationIntent,
    profile: CapabilityProfile,
    capabilities: frozenset[str],
) -> str:
    binding = "|".join(
        (
            str(job.job_id),
            role.value,
            str(attempt),
            workspace_ref,
            integration_intent.value,
            profile.value,
            ",".join(sorted(capabilities)),
        )
    )
    digest = hashlib.sha256(binding.encode("utf-8")).hexdigest()
    return f"factory-{digest[:32]}"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise FactoryLeaseDenied("factory leases require a UTC clock")
    return value.astimezone(timezone.utc)
