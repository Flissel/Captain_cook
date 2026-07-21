from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agenten.agent_factory.contracts import AgentFactoryJobV3, FactoryRole
from agenten.agent_factory.execution_policy import FactoryExecutionPolicyV1
from agenten.agent_factory.leases import (
    FactoryLeaseDenied,
    issue_factory_lease,
    validate_factory_lease,
)
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


def v3_job(*, live: bool) -> AgentFactoryJobV3:
    v1 = job()
    policy = FactoryExecutionPolicyV1.model_validate(
        {
            "schema": "captain.factory-execution-policy.v1",
            "mode": "release",
            "live_execution": live,
            "max_cost_usd": "5.00" if live else "0.00",
            "max_runtime_seconds": 900,
            "required_live_runs": 3 if live else 0,
            "allowed_models": ["approved-model-id"] if live else [],
            "live_capabilities": [
                "model.invoke",
                "docker.run",
                "database.captain_test",
                "browser.use",
                "computer.use",
            ]
            if live
            else [],
            "sandbox_mode": "workspace_write",
        }
    )
    return AgentFactoryJobV3.model_validate(
        v1.model_dump(mode="json", by_alias=True)
        | {
            "schema": "captain.agent-factory-job.v3",
            "input_ref": {
                "uri": f"artifact://input/{'a' * 64}",
                "sha256": "a" * 64,
                "media_type": "text/markdown",
            },
            "compiled_spec_ref": {
                "uri": f"artifact://compiled/{'b' * 64}",
                "sha256": "b" * 64,
                "media_type": "application/json",
            },
            "dependency_graph_ref": {
                "uri": f"artifact://graph/{'c' * 64}",
                "sha256": "c" * 64,
                "media_type": "application/json",
            },
            "private_holdout_refs": [
                {
                    "schema_name": "captain.private-holdout-ref.v1",
                    "holdout_id": "holdout-111111111111",
                    "uri": "holdout://holdout-111111111111",
                    "sha256": "d" * 64,
                }
            ],
            "deadline_at": NOW + timedelta(minutes=15),
            "execution_policy": policy.model_dump(mode="json", by_alias=True),
        }
    )


def test_v3_real_case_tester_receives_exact_explicit_live_capabilities() -> None:
    factory_job = v3_job(live=True)
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
    )

    assert {
        "model.invoke",
        "docker.run",
        "database.captain_test",
        "browser.use",
        "computer.use",
    }.issubset(lease.capabilities)
    assert validate_factory_lease(
        lease,
        job=factory_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        now=NOW,
    ) == lease


def test_v3_offline_and_non_tester_roles_receive_no_live_capabilities() -> None:
    live_job = v3_job(live=True)
    offline_lease = issue_factory_lease(
        job=v3_job(live=False),
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
    )
    tool_lease = issue_factory_lease(
        job=live_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
    )

    for capability in (
        "model.invoke",
        "docker.run",
        "database.captain_test",
        "browser.use",
        "computer.use",
    ):
        assert capability not in offline_lease.capabilities
        assert capability not in tool_lease.capabilities


def test_v3_lease_validation_rejects_one_injected_capability() -> None:
    factory_job = v3_job(live=True)
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=NOW,
    )
    tampered = lease.model_copy(
        update={"capabilities": lease.capabilities + ("unreleased.effect",)}
    )

    with pytest.raises(FactoryLeaseDenied, match="do not match"):
        validate_factory_lease(
            tampered,
            job=factory_job,
            role=FactoryRole.REAL_CASE_TESTER,
            attempt=1,
            now=NOW,
        )
