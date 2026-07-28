from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import FactoryLeaseDenied, issue_factory_lease
from agenten.agent_factory.state_machine import FactoryAction, FactoryActionKind
from agenten.agent_runtime.contracts import IntegrationIntent
from gateway.agent_factory_dispatch_runner import GatewayNextActionLeaseIssuer
from tests.agent_factory.test_release_gate import workflow_job


NOW = datetime(2026, 7, 20, 11, tzinfo=timezone.utc)


class RecordingGatewayStore:
    def __init__(self, job, *, leases=(), batch=None, reject_lease=False) -> None:
        self.job = job
        self.leases = list(leases)
        self.batch = batch
        self.recorded = []
        self.reject_lease = reject_lease

    def factory_job(self, job_id):
        assert job_id == self.job.job_id
        return type("Bundle", (), {"leases": tuple(self.leases)})()

    def record_factory_lease(self, lease):
        self.recorded.append(lease)
        if self.reject_lease:
            raise ValueError("not the current next action")
        same_identity = next(
            (item for item in self.leases if item.lease_id == lease.lease_id),
            None,
        )
        if same_identity is not None and same_identity != lease:
            raise ValueError("lease identity already has different content")
        replayed = lease in self.leases
        if not replayed:
            self.leases.append(lease)
        return type("Receipt", (), {"replayed": replayed})()

    def bundle(self, batch_id: str):
        if self.batch is None or self.batch["batch_id"] != batch_id:
            raise KeyError(batch_id)
        return self.batch


def _action(kind: FactoryActionKind) -> FactoryAction:
    return FactoryAction(kind=kind, attempt=1)


def test_gateway_issuer_reuses_the_active_next_action_lease() -> None:
    job = workflow_job(mode="demo")
    lease = issue_factory_lease(
        job=job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/existing",
        now=NOW,
    )
    store = RecordingGatewayStore(job, leases=(lease,))

    recovered = GatewayNextActionLeaseIssuer(
        store=store,
        workspace_namespace="business-benchmark-demo",
    ).ensure_for(
        job,
        _action(FactoryActionKind.DISPATCH_AGENT_ARCHITECT),
        FactoryRole.AGENT_ARCHITECT,
        NOW,
    )

    assert recovered == lease
    assert store.recorded == [lease]


def test_gateway_revalidates_a_reused_lease_under_the_current_action_lock() -> None:
    job = workflow_job(mode="demo")
    lease = issue_factory_lease(
        job=job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/existing",
        now=NOW,
    )
    store = RecordingGatewayStore(job, leases=(lease,), reject_lease=True)

    with pytest.raises(FactoryLeaseDenied, match="Gateway rejected"):
        GatewayNextActionLeaseIssuer(
            store=store,
            workspace_namespace="business-benchmark-demo",
        ).ensure_for(
            job,
            _action(FactoryActionKind.DISPATCH_AGENT_ARCHITECT),
            FactoryRole.AGENT_ARCHITECT,
            NOW,
        )

    assert store.recorded == [lease]


def test_gateway_issuer_records_a_new_lease_that_gateway_must_admit() -> None:
    job = workflow_job(mode="demo")
    store = RecordingGatewayStore(job)

    issued = GatewayNextActionLeaseIssuer(
        store=store,
        workspace_namespace="business-benchmark-demo",
    ).ensure_for(
        job,
        _action(FactoryActionKind.DISPATCH_REAL_CASE_TESTER),
        FactoryRole.REAL_CASE_TESTER,
        NOW,
    )

    assert store.recorded == [issued]
    assert issued.role is FactoryRole.REAL_CASE_TESTER
    assert issued.integration_intent is IntegrationIntent.NONE
    assert "/dispatch_real_case_tester/1/" in issued.workspace_ref


def test_gateway_issuer_requires_a_released_n8n_work_batch_before_n8n_lease() -> None:
    job = workflow_job(mode="demo")
    store = RecordingGatewayStore(job)
    issuer = GatewayNextActionLeaseIssuer(
        store=store,
        workspace_namespace="business-benchmark-demo",
        n8n_work_batches={job.job_id: "renewal-missing"},
    )

    with pytest.raises(FactoryLeaseDenied, match="released n8n WorkBatch"):
        issuer.ensure_for(
            job,
            _action(FactoryActionKind.DISPATCH_TOOL_INTEGRATOR),
            FactoryRole.TOOL_INTEGRATOR,
            NOW,
        )

    assert store.recorded == []


def test_gateway_issuer_grants_n8n_only_for_tool_integrator_with_valid_batch() -> None:
    job = workflow_job(mode="demo")
    batch_id = f"renewal-{job.job_id.hex[:24]}"
    store = RecordingGatewayStore(
        job,
        batch={
            "batch_id": batch_id,
            "title": "Renewal read",
            "goal": "Read renewal context",
            "subtask_ids": ["renewal_context_read"],
            "target": "n8n",
            "runtime": "n8n",
            "runtime_version": "v1",
            "interface_schema": "captain-n8n-artifact/v1",
            "capability_tags": ["n8n-builder"],
            "constraints": ["read only"],
            "acceptance_criteria": [
                {
                    "assertion_id": "renewal-context-read",
                    "description": "Returns redacted context",
                    "kind": "output_contains",
                    "path": "status",
                    "expected": "ok",
                }
            ],
        },
    )
    issuer = GatewayNextActionLeaseIssuer(
        store=store,
        workspace_namespace="business-benchmark-demo",
        n8n_work_batches={job.job_id: batch_id},
    )

    lease = issuer.ensure_for(
        job,
        _action(FactoryActionKind.DISPATCH_TOOL_INTEGRATOR),
        FactoryRole.TOOL_INTEGRATOR,
        NOW,
    )

    assert lease.integration_intent is IntegrationIntent.N8N
    assert "mcp.n8n" in lease.capabilities


def test_tool_integrator_leases_are_action_scoped_at_the_same_timestamp() -> None:
    job = workflow_job(mode="demo")
    batch_id = f"renewal-{job.job_id.hex[:24]}"
    batch = {
        "batch_id": batch_id,
        "title": "Renewal read",
        "goal": "Read renewal context",
        "subtask_ids": ["renewal_context_read"],
        "target": "n8n",
        "runtime": "n8n",
        "runtime_version": "v1",
        "interface_schema": "captain-n8n-artifact/v1",
        "capability_tags": ["n8n-builder"],
        "constraints": ["read only"],
        "acceptance_criteria": [
            {
                "assertion_id": "renewal-context-read",
                "description": "Returns redacted context",
                "kind": "output_contains",
                "path": "status",
                "expected": "ok",
            }
        ],
    }
    store = RecordingGatewayStore(job, batch=batch)
    issuer = GatewayNextActionLeaseIssuer(
        store=store,
        workspace_namespace="business-benchmark-demo",
        n8n_work_batches={job.job_id: batch_id},
    )

    integrator = issuer.ensure_for(
        job,
        _action(FactoryActionKind.DISPATCH_TOOL_INTEGRATOR),
        FactoryRole.TOOL_INTEGRATOR,
        NOW,
    )
    assert issuer.active(
        job, FactoryRole.TOOL_INTEGRATOR, 1, NOW
    ) == integrator
    forge = issuer.ensure_for(
        job,
        _action(FactoryActionKind.SUBMIT_FORGE_JOB),
        FactoryRole.TOOL_INTEGRATOR,
        NOW,
    )
    assert issuer.active(
        job, FactoryRole.TOOL_INTEGRATOR, 1, NOW
    ) == forge
    build = issuer.ensure_for(
        job,
        _action(FactoryActionKind.DISPATCH_BUILD_VALIDATOR),
        FactoryRole.TOOL_INTEGRATOR,
        NOW,
    )
    assert issuer.active(
        job, FactoryRole.TOOL_INTEGRATOR, 1, NOW
    ) == build

    assert integrator.integration_intent is IntegrationIntent.N8N
    assert forge.integration_intent is IntegrationIntent.NONE
    assert build.integration_intent is IntegrationIntent.NONE
    assert len({item.workspace_ref for item in (integrator, forge, build)}) == 3
    assert store.recorded == [
        integrator,
        integrator,
        forge,
        forge,
        build,
        build,
    ]


def test_expired_action_lease_is_renewed_with_a_unique_lease_id() -> None:
    job = workflow_job(mode="demo")
    action = _action(FactoryActionKind.DISPATCH_BUILD_VALIDATOR)
    expired = issue_factory_lease(
        job=job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref=(
            f"workspace://business-benchmark-demo/{job.job_id}/"
            f"dispatch_build_validator/1/{(NOW - timedelta(minutes=16)).strftime('%Y%m%dT%H%M%S%fZ')}"
        ),
        now=NOW - timedelta(minutes=16),
    )
    store = RecordingGatewayStore(job, leases=(expired,))

    renewed = GatewayNextActionLeaseIssuer(
        store=store,
        workspace_namespace="business-benchmark-demo",
    ).ensure_for(
        job,
        action,
        FactoryRole.TOOL_INTEGRATOR,
        NOW,
    )

    assert renewed.lease_id != expired.lease_id
    assert renewed.workspace_ref != expired.workspace_ref
    assert renewed.integration_intent is IntegrationIntent.NONE
    assert store.recorded == [renewed]


def test_dispatcher_lease_lookup_fails_without_immediate_ensure() -> None:
    job = workflow_job(mode="demo")
    issuer = GatewayNextActionLeaseIssuer(
        store=RecordingGatewayStore(job),
        workspace_namespace="business-benchmark-demo",
    )

    with pytest.raises(FactoryLeaseDenied, match="immediately authorized"):
        issuer.active(job, FactoryRole.TOOL_INTEGRATOR, 1, NOW)


def test_immediately_authorized_dispatcher_lease_is_single_use() -> None:
    job = workflow_job(mode="demo")
    issuer = GatewayNextActionLeaseIssuer(
        store=RecordingGatewayStore(job),
        workspace_namespace="business-benchmark-demo",
    )
    issued = issuer.ensure_for(
        job,
        _action(FactoryActionKind.DISPATCH_BUILD_VALIDATOR),
        FactoryRole.TOOL_INTEGRATOR,
        NOW,
    )

    assert issuer.active(
        job, FactoryRole.TOOL_INTEGRATOR, 1, NOW
    ) == issued
    with pytest.raises(FactoryLeaseDenied, match="immediately authorized"):
        issuer.active(job, FactoryRole.TOOL_INTEGRATOR, 1, NOW)
