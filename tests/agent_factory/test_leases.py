from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import FactoryLeaseDenied, issue_factory_lease, validate_factory_lease
from tests.agent_factory.test_state_machine import job


NOW = datetime(2026, 7, 19, 10, tzinfo=timezone.utc)


def test_issued_lease_exactly_matches_role_capabilities() -> None:
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
    )

    assert "codex.run" in lease.capabilities
    assert validate_factory_lease(
        lease, job=factory_job, role=FactoryRole.TOOL_INTEGRATOR, attempt=1, now=NOW
    ) == lease


def test_expired_or_cross_role_lease_is_denied() -> None:
    factory_job = job()
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
    )

    with pytest.raises(FactoryLeaseDenied, match="different role"):
        validate_factory_lease(
            lease, job=factory_job, role=FactoryRole.QUALITY_WARDEN, attempt=1, now=NOW
        )
    with pytest.raises(FactoryLeaseDenied, match="not active"):
        validate_factory_lease(
            lease,
            job=factory_job,
            role=FactoryRole.AGENT_ARCHITECT,
            attempt=1,
            now=NOW + timedelta(minutes=15),
        )
