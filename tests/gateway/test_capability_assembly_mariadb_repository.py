from __future__ import annotations

import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from blockchain.mariadb_storage import MariaDBStorage
from gateway.capability_assembly_controller import CapabilityAssemblyConflict
from gateway.capability_assembly_repository import GatewayCapabilityAssemblyRepository
from gateway.store import GatewayStore
from tests.gateway.test_capability_assembly_controller import (
    ASSEMBLY_ID,
    _SharedReceiptReconciliation,
    _controller,
    _crash_first_slice,
    _id,
    _submission,
)
from gateway.capability_assembly_contracts import CapabilityAssemblyAdvanceRequest
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")


def _clean_assembly_tables(storage: MariaDBStorage) -> None:
    with storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM capability_assembly_retry_authorizations")
            cursor.execute("DELETE FROM capability_assembly_checkpoints")
            cursor.execute("DELETE FROM capability_assembly_heads")


def test_mariadb_retry_receipt_is_atomic_across_independent_connections() -> None:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    first_storage = MariaDBStorage(TEST_DSN)
    second_storage = MariaDBStorage(TEST_DSN)
    first_repository = GatewayCapabilityAssemblyRepository(GatewayStore(first_storage))
    second_repository = GatewayCapabilityAssemblyRepository(GatewayStore(second_storage))
    _clean_assembly_tables(first_storage)
    barrier = Barrier(2)
    receipt_id = _id("mariadb-global-retry-receipt")

    class RacingReconciliation(_SharedReceiptReconciliation):
        async def reconcile(self, request):
            outcome = await super().reconcile(request)
            barrier.wait(timeout=10)
            return outcome

    reconciliation = RacingReconciliation(receipt_id)
    first, _, _, first_factory, *_ = _controller(
        first_repository,
        reconciliation=reconciliation,
    )
    second, _, _, second_factory, *_ = _controller(
        second_repository,
        reconciliation=reconciliation,
    )
    second_assembly_id = _id("mariadb-second-assembly")
    first.submit(_submission(assembly_id=ASSEMBLY_ID))
    second.submit(
        _submission(assembly_id=second_assembly_id, idempotency_key="c" * 64)
    )
    first_request = CapabilityAssemblyAdvanceRequest(
        assembly_id=ASSEMBLY_ID,
        operation_id=_id("mariadb-first-operation"),
        request_sha256=first.projection(ASSEMBLY_ID).request_sha256,
        maximum_slices=1,
    )
    second_request = CapabilityAssemblyAdvanceRequest(
        assembly_id=second_assembly_id,
        operation_id=_id("mariadb-second-operation"),
        request_sha256=second.projection(second_assembly_id).request_sha256,
        maximum_slices=1,
    )
    _crash_first_slice(first, first_factory, first_request)
    _crash_first_slice(second, second_factory, second_request)
    first_restart, _, _, first_restart_factory, *_ = _controller(
        GatewayCapabilityAssemblyRepository(GatewayStore(MariaDBStorage(TEST_DSN))),
        reconciliation=reconciliation,
    )
    second_restart, _, _, second_restart_factory, *_ = _controller(
        GatewayCapabilityAssemblyRepository(GatewayStore(MariaDBStorage(TEST_DSN))),
        reconciliation=reconciliation,
    )
    before = {
        ASSEMBLY_ID: len(first_repository.history(ASSEMBLY_ID)),
        second_assembly_id: len(second_repository.history(second_assembly_id)),
    }

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = (
            pool.submit(lambda: asyncio.run(first_restart.advance(first_request))),
            pool.submit(lambda: asyncio.run(second_restart.advance(second_request))),
        )
        results = tuple(future.exception(timeout=20) for future in futures)

    assert sum(isinstance(result, CapabilityAssemblyConflict) for result in results) == 1
    growth_and_effects = (
        (
            len(first_repository.history(ASSEMBLY_ID)) - before[ASSEMBLY_ID],
            len(first_restart_factory.calls),
        ),
        (
            len(second_repository.history(second_assembly_id))
            - before[second_assembly_id],
            len(second_restart_factory.calls),
        ),
    )
    assert sorted(growth_and_effects) == [(0, 0), (4, 1)]
    _clean_assembly_tables(first_storage)
