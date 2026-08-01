"""Gateway-owned MariaDB composition for technical revalidation issuance."""

from __future__ import annotations

from dataclasses import dataclass

from agenten.agent_factory.service import FactoryCoordinator
from blockchain.mariadb_storage import MariaDBStorage
from gateway.factory_repository import GatewayFactoryRepository
from gateway.store import GatewayStore


@dataclass(frozen=True)
class FactoryTechnicalRevalidationComposition:
    store: GatewayStore
    repository: GatewayFactoryRepository
    coordinator: FactoryCoordinator


def compose_factory_technical_revalidation(
    test_mariadb_dsn: str,
) -> FactoryTechnicalRevalidationComposition:
    """Keep the concrete database adapter behind the Gateway boundary."""

    store = GatewayStore(MariaDBStorage(test_mariadb_dsn))
    repository = GatewayFactoryRepository(store)
    return FactoryTechnicalRevalidationComposition(
        store=store,
        repository=repository,
        coordinator=FactoryCoordinator(repository),
    )


__all__ = [
    "FactoryTechnicalRevalidationComposition",
    "compose_factory_technical_revalidation",
]
