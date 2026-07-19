from __future__ import annotations

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.service import FactoryCoordinator
from gateway.factory_repository import GatewayFactoryLeases, GatewayFactoryRepository
from tests.agent_factory.test_state_machine import block, job


class Store:
    def __init__(self) -> None:
        self.jobs = {}
        self.events = {}

    def record_factory_job(self, factory_job):
        self.jobs.setdefault(factory_job.job_id, factory_job)
        return type("Receipt", (), {"replayed": False})()

    def record_factory_block(self, evidence):
        self.events.setdefault(evidence.job_id, []).append(evidence)
        return type("Receipt", (), {"replayed": False})()

    def factory_job(self, job_id):
        return type("Projection", (), {"job": self.jobs[job_id], "blocks": tuple(self.events.get(job_id, ()))})()


def test_gateway_adapter_runs_coordinator_against_gateway_store_shape() -> None:
    coordinator = FactoryCoordinator(GatewayFactoryRepository(Store()))
    factory_job = job()

    coordinator.register(factory_job)
    coordinator.record(block(FactoryPhase.FORGE_REQUESTED))

    assert coordinator.projection(factory_job.job_id).phase is FactoryPhase.FORGE_REQUESTED


def test_gateway_leases_resolve_only_the_current_role_attempt() -> None:
    store = Store()
    factory_job = job()
    store.jobs[factory_job.job_id] = factory_job
    lease = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/support-triage",
        now=factory_job.occurred_at,
    )
    store.factory_leases = (lease,)
    store.factory_job = lambda _job_id: type(
        "Projection", (), {"job": factory_job, "blocks": (), "leases": store.factory_leases}
    )()

    resolved = GatewayFactoryLeases(store).active(
        factory_job, FactoryRole.AGENT_ARCHITECT, 1, factory_job.occurred_at
    )

    assert resolved.lease_id == lease.lease_id
