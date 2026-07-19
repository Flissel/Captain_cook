from __future__ import annotations

from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.service import FactoryCoordinator
from gateway.factory_repository import GatewayFactoryRepository
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
