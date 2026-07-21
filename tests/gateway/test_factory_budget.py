from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import HTTPException

from agenten.agent_factory.execution_budget import (
    FactoryBudgetProjection,
    FactoryBudgetReservationV1,
    FactoryBudgetWriteReceipt,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.skill_workflow_contracts import CodebaseInventoryV1
from agenten.agent_factory.state_machine import FactoryLifecycleStatus, FactoryProjection
from gateway.contracts import FactoryBudgetReservationWriteReceipt
from gateway.factory_repository import GatewayFactoryBudgetLedger
from gateway.store import GatewayStore
from blockchain.mariadb_storage import MariaDBStorage
from tests.agent_factory.test_execution_budget import job_v3, usage_payload
from tests.agent_factory.test_state_machine import block, job
from tests.agent_factory.test_capability_resolution import job as job_v2
from tests.agent_factory.test_skill_workflow_contracts import inventory_payload
from tests.support.mariadb import assert_isolated_test_database


NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
TEST_DSN = os.getenv("TEST_MARIADB_DSN")


class BudgetStore:
    def __init__(self, factory_job) -> None:
        self.job = factory_job
        self.reservation_request = None
        self.usage_receipt = None
        self.release_request = None
        self.reservation = None

    def reserve_factory_budget(self, request):
        self.reservation_request = request
        self.reservation = FactoryBudgetReservationV1(
            schema_name="captain.factory-budget-reservation.v1",
            reservation_id=request.reservation_id,
            job_id=request.job_id,
            correlation_id=request.correlation_id,
            subject_version=request.subject_version,
            execution_policy_sha256=request.execution_policy_sha256,
            attempt=request.attempt,
            requested_usd=request.requested_usd,
            reserved_at=request.reserved_at,
            expires_at=request.expires_at,
        )
        return FactoryBudgetReservationWriteReceipt(
            event_id=request.reservation_id,
            job_id=request.job_id,
            replayed=False,
            reservation=self.reservation,
        )

    def record_factory_usage(self, receipt):
        self.usage_receipt = receipt
        return FactoryBudgetWriteReceipt(
            event_id=receipt.receipt_id,
            job_id=receipt.job_id,
            replayed=False,
        )

    def release_factory_budget(self, request):
        self.release_request = request
        return FactoryBudgetWriteReceipt(
            event_id=request.release_id,
            job_id=request.job_id,
            replayed=False,
        )

    def factory_budget(self, job_id):
        return FactoryBudgetProjection(
            job_id=job_id,
            limit_usd=Decimal("5.00"),
            consumed_usd=Decimal("0.00"),
            reserved_usd=Decimal("0.00"),
            remaining_usd=Decimal("5.00"),
        )


def test_gateway_budget_adapter_binds_job_and_round_trips_port_calls(job_v3) -> None:
    store = BudgetStore(job_v3)
    ledger = GatewayFactoryBudgetLedger(store)

    reservation = ledger.reserve(
        job_v3,
        attempt=2,
        requested_usd=Decimal("1.25"),
        now=NOW,
    )
    GatewayStore._assert_budget_reservation(job_v3, reservation)
    receipt = FactoryUsageReceiptV1.model_validate(
        usage_payload(reservation, cost_usd="0.80")
    )
    usage_write = ledger.record_usage(job_v3, reservation, receipt)
    release_reservation = reservation.model_copy(
        update={"reservation_id": UUID("30000000-0000-0000-0000-000000000001")}
    )
    release_write = ledger.release(
        job_v3,
        release_reservation,
        now=NOW + timedelta(seconds=2),
        reason="unused",
    )

    assert store.reservation_request.job_id == job_v3.job_id
    assert store.reservation_request.correlation_id == job_v3.correlation_id
    assert store.reservation_request.subject_version == job_v3.subject_version
    assert store.reservation_request.attempt == 2
    assert store.usage_receipt == receipt
    assert store.release_request.reservation_id == release_reservation.reservation_id
    assert usage_write.replayed is False
    assert release_write.replayed is False
    assert ledger.projection(job_v3.job_id).remaining_usd == Decimal("5.00")


def test_gateway_budget_reservation_identity_is_stable_for_retry(job_v3) -> None:
    first_store = BudgetStore(job_v3)
    second_store = BudgetStore(job_v3)

    first = GatewayFactoryBudgetLedger(first_store).reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )
    second = GatewayFactoryBudgetLedger(second_store).reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )

    assert first.reservation_id == second.reservation_id
    assert first.execution_policy_sha256 == second.execution_policy_sha256


def test_gateway_budget_rejects_expired_windows_and_terminal_jobs(job_v3) -> None:
    reservation = GatewayFactoryBudgetLedger(BudgetStore(job_v3)).reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )
    expired = reservation.model_copy(update={"reserved_at": job_v3.deadline_at})

    with pytest.raises(HTTPException, match="V3 job policy"):
        GatewayStore._assert_budget_reservation(job_v3, expired)

    terminal = FactoryProjection.from_job(
        GatewayStore._factory_lifecycle_job(job_v3)
    ).model_copy(
        update={"status": FactoryLifecycleStatus.ESCALATED}
    )
    with pytest.raises(HTTPException, match="terminal"):
        GatewayStore._assert_factory_effects_open(terminal, effect="paid effects")


@pytest.fixture
def mariadb_store() -> GatewayStore:
    if TEST_DSN is None:
        pytest.skip("TEST_MARIADB_DSN is not configured")
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    store = GatewayStore(storage)
    storage.clear()
    yield store
    storage.clear()


def test_mariadb_budget_replay_conflict_and_restart_projection(
    mariadb_store: GatewayStore,
    job_v3,
) -> None:
    mariadb_store.record_factory_job(job_v3)
    adapter = GatewayFactoryBudgetLedger(mariadb_store)
    reservation = adapter.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )

    replay = mariadb_store.reserve_factory_budget(reservation)
    assert replay.replayed is True
    with pytest.raises(HTTPException, match="different content"):
        mariadb_store.reserve_factory_budget(
            reservation.model_copy(update={"requested_usd": Decimal("2.01")})
        )

    usage = FactoryUsageReceiptV1.model_validate(
        usage_payload(reservation, cost_usd="1.25")
    )
    mariadb_store.record_factory_usage(usage)
    restarted = GatewayStore(mariadb_store.storage)
    projection = restarted.factory_budget(job_v3.job_id)
    assert projection.consumed_usd == Decimal("1.25")
    assert projection.reserved_usd == Decimal("0")
    assert projection.remaining_usd == Decimal("3.75")


def test_mariadb_budget_refuses_unknown_model_and_usage_above_reservation(
    mariadb_store: GatewayStore,
    job_v3,
) -> None:
    mariadb_store.record_factory_job(job_v3)
    reservation = GatewayFactoryBudgetLedger(mariadb_store).reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )

    with pytest.raises(HTTPException, match="unapproved model"):
        mariadb_store.record_factory_usage(
            FactoryUsageReceiptV1.model_validate(
                usage_payload(reservation, model="unknown-model")
            )
        )


@pytest.mark.parametrize("historical", (job(), job_v2()))
def test_mariadb_budget_refuses_paid_effects_for_historical_job_versions(
    mariadb_store: GatewayStore,
    historical,
) -> None:
    mariadb_store.record_factory_job(historical)
    expires_at = getattr(
        historical,
        "deadline_at",
        historical.occurred_at + timedelta(minutes=5),
    )
    reservation = FactoryBudgetReservationV1(
        schema_name="captain.factory-budget-reservation.v1",
        reservation_id=UUID("30000000-0000-0000-0000-000000000010"),
        job_id=historical.job_id,
        correlation_id=historical.correlation_id,
        subject_version=historical.subject_version,
        execution_policy_sha256="f" * 64,
        attempt=1,
        requested_usd=Decimal("1.00"),
        reserved_at=historical.occurred_at,
        expires_at=expires_at,
    )

    with pytest.raises(HTTPException, match="V3"):
        mariadb_store.reserve_factory_budget(reservation)


def test_mariadb_budget_refuses_a_missing_job(job_v3) -> None:
    if TEST_DSN is None:
        pytest.skip("TEST_MARIADB_DSN is not configured")
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    store = GatewayStore(storage)
    storage.clear()
    try:
        reservation = GatewayFactoryBudgetLedger(BudgetStore(job_v3)).reserve(
            job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
        )
        with pytest.raises(HTTPException, match="not found"):
            store.reserve_factory_budget(reservation)
    finally:
        storage.clear()


def test_mariadb_workflow_artifact_is_append_only_and_restart_readable(
    mariadb_store: GatewayStore,
    job_v3,
) -> None:
    artifact = CodebaseInventoryV1.model_validate(inventory_payload())
    factory_job = job_v3.model_copy(
        update={
            "job_id": artifact.job_id,
            "correlation_id": artifact.correlation_id,
            "subject_version": artifact.subject_version,
            "occurred_at": artifact.invocation.lease.issued_at,
            "deadline_at": artifact.invocation.lease.expires_at + timedelta(minutes=5),
            "input_ref": artifact.invocation.input_ref,
            "acceptance_assertion_ids": artifact.acceptance_assertion_ids,
        }
    )
    forge = block(FactoryPhase.FORGE_REQUESTED).model_copy(
        update={
            "job_id": factory_job.job_id,
            "correlation_id": factory_job.correlation_id,
            "subject_version": factory_job.subject_version,
            "occurred_at": factory_job.occurred_at,
        }
    )
    mariadb_store.record_factory_job(factory_job)
    mariadb_store.record_factory_block(forge)
    mariadb_store.record_factory_lease(artifact.invocation.lease)

    first = mariadb_store.record_factory_workflow_artifact(artifact)
    replay = mariadb_store.record_factory_workflow_artifact(artifact)
    with pytest.raises(HTTPException, match="different content"):
        mariadb_store.record_factory_workflow_artifact(
            artifact.model_copy(update={"autogen_version": "0.7.6"})
        )

    restarted = GatewayStore(mariadb_store.storage)
    assert first.replayed is False
    assert replay.replayed is True
    assert restarted.factory_workflow_artifacts(factory_job.job_id) == (artifact,)


def test_mariadb_budget_serializes_concurrent_overspend(
    mariadb_store: GatewayStore,
    job_v3,
) -> None:
    mariadb_store.record_factory_job(job_v3)

    def reserve(offset: int):
        try:
            return GatewayFactoryBudgetLedger(mariadb_store).reserve(
                job_v3,
                attempt=1,
                requested_usd=Decimal("3.00"),
                now=NOW + timedelta(microseconds=offset),
            )
        except HTTPException as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, (1, 2)))

    assert sum(isinstance(item, FactoryBudgetReservationV1) for item in outcomes) == 1
    assert sum(isinstance(item, HTTPException) for item in outcomes) == 1
    assert mariadb_store.factory_budget(job_v3.job_id).remaining_usd == Decimal("2.00")
    with pytest.raises(HTTPException, match="exceeds"):
        mariadb_store.record_factory_usage(
            FactoryUsageReceiptV1.model_validate(
                usage_payload(
                    reservation,
                    receipt_id=UUID("20000000-0000-0000-0000-000000000002"),
                    cost_usd="1.01",
                )
            )
        )
