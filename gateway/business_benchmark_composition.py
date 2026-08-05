"""Gateway-owned production ports for the Captain business benchmark."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from pymysql.err import MySQLError

from blockchain.mariadb_storage import MariaDBStorage
from agenten.agent_factory.business_benchmark_bootstrap import (
    CaptainBusinessBenchmarkExecutionPolicyBuilderPort,
    CaptainBusinessBenchmarkExecutorBuilderPort,
    ProductionBusinessBenchmarkBootstrapConfig,
    ProductionBusinessBenchmarkBootstrapPorts,
    compose_production_business_benchmark_composition,
)
from agenten.agent_factory.business_benchmark_demo_provisioning import (
    assert_local_captain_test_dsn,
)
from agenten.agent_factory.business_benchmark_live import (
    LiveBusinessBenchmarkSettings,
    ProductionBusinessBenchmarkCompositionPort,
)
from agenten.agent_factory.business_benchmark_production import (
    BusinessBenchmarkCandidateAuthorityPort,
)
from gateway.factory_repository import (
    GatewayFactoryBudgetLedger,
    GatewayFactoryLeases,
    GatewayFactoryRepository,
)
from gateway.store import GatewayStore


class GatewayBusinessBenchmarkCompositionError(RuntimeError):
    """Expected failure while opening the isolated Captain Gateway store."""


class GatewayBusinessBenchmarkCompositionAuthority:
    """Own the one MariaDB connection and expose only typed Captain ports.

    Agent-runtime modules receive repository, lease, budget, and skill-catalog
    ports.  They never receive a DSN or import the Gateway implementation.
    """

    def __init__(self, dsn: str) -> None:
        assert_local_captain_test_dsn(dsn)
        try:
            store = GatewayStore(MariaDBStorage(dsn))
        except MySQLError as exc:
            raise GatewayBusinessBenchmarkCompositionError(
                "Captain benchmark Gateway MariaDB is unavailable"
            ) from exc
        self._store = store
        self.repository = GatewayFactoryRepository(store)
        self.leases = GatewayFactoryLeases(store)
        self.budget = GatewayFactoryBudgetLedger(store)

    @property
    def runtime_store(self) -> GatewayStore:
        """Expose the store only to sibling Gateway-owned runtime adapters."""

        return self._store

    def compose(
        self,
        settings: LiveBusinessBenchmarkSettings,
        *,
        config: ProductionBusinessBenchmarkBootstrapConfig,
        executor_builder: CaptainBusinessBenchmarkExecutorBuilderPort,
        execution_policy_builder: CaptainBusinessBenchmarkExecutionPolicyBuilderPort,
        clock: Callable[[], datetime],
        candidate_authority: BusinessBenchmarkCandidateAuthorityPort | None = None,
    ) -> ProductionBusinessBenchmarkCompositionPort:
        """Inject one store-backed authority graph into the pure composition."""

        return compose_production_business_benchmark_composition(
            settings,
            config=config,
            ports=ProductionBusinessBenchmarkBootstrapPorts(
                gateway_repository=self.repository,
                released_skills=self.repository,
                leases=self.leases,
                executor_builder=executor_builder,
                execution_policy_builder=execution_policy_builder,
                clock=clock,
                candidate_authority=candidate_authority,
            ),
        )


__all__ = [
    "GatewayBusinessBenchmarkCompositionAuthority",
    "GatewayBusinessBenchmarkCompositionError",
]
