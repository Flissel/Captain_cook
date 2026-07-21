from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from agenten.agent_factory.execution_budget import (
    BudgetExhausted,
    FactoryBudgetProjection,
    FactoryBudgetReservationV1,
    FactoryBudgetWriteReceipt,
    FactoryUsageReceiptV1,
)
from agenten.agent_factory.contracts import FactoryPhase
from agenten.agent_factory.contracts import FactoryRole
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_factory.skill_workflow_contracts import CodebaseInventoryV1
from agenten.agent_factory.state_machine import FactoryLifecycleStatus, FactoryProjection
from gateway.contracts import FactoryBudgetReservationWriteReceipt
from gateway.contracts import FactoryUsageSubmissionV2
from gateway.app import create_app
from gateway.auth import GatewayRole, require_actor
from gateway.factory_repository import GatewayFactoryBudgetLedger
from gateway.settings import GatewaySettings
from gateway.store import GatewayStore
from blockchain.mariadb_storage import MariaDBStorage
from tests.agent_factory.test_execution_budget import job_v3, usage_payload
from tests.agent_factory.test_state_machine import block, job
from tests.agent_factory.test_capability_resolution import job as job_v2
from tests.agent_factory.test_skill_workflow_contracts import inventory_payload
from tests.support.mariadb import assert_isolated_test_database


NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)
TEST_DSN = os.getenv("TEST_MARIADB_DSN")


class Mirror:
    def enqueue_nowait(self, _: dict[str, object]) -> None:
        return None


def validation_only_app(actor: GatewayRole):
    app = create_app(
        mirror=Mirror(),
        settings=GatewaySettings(
            ledger_dsn=SecretStr("mariadb://unused/validation_only"),
            captain_gateway_token=SecretStr("captain-test-token"),
            worker_gateway_token=SecretStr("worker-test-token"),
        ),
    )

    async def selected_actor(_: Request) -> GatewayRole:
        return actor

    app.dependency_overrides[require_actor] = selected_actor
    return app


class BudgetStore:
    def __init__(self, factory_job) -> None:
        self.job = factory_job
        self.reservation_request = None
        self.usage_receipt = None
        self.usage_submission = None
        self.release_request = None
        self.reservation = None
        self.lease = issue_factory_lease(
            job=factory_job,
            role=FactoryRole.REAL_CASE_TESTER,
            attempt=2,
            workspace_ref="workspace://factory/budget-test",
            now=factory_job.occurred_at,
        )

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

    def record_factory_usage(self, submission):
        self.usage_submission = submission
        self.usage_receipt = submission.receipt
        return FactoryBudgetWriteReceipt(
            event_id=submission.receipt.receipt_id,
            job_id=submission.receipt.job_id,
            replayed=False,
        )

    def factory_job(self, _job_id):
        return type("Factory", (), {"leases": (self.lease,)})()

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
    assert store.usage_submission.subject_version == job_v3.subject_version
    assert store.usage_submission.lease_id == store.lease.lease_id
    assert store.release_request.reservation_id == release_reservation.reservation_id
    assert usage_write.replayed is False
    assert release_write.replayed is False
    assert ledger.projection(job_v3.job_id).remaining_usd == Decimal("5.00")


@pytest.mark.parametrize(
    ("path", "actor"),
    (
        ("/v1/factory/jobs", GatewayRole.CAPTAIN),
        ("/v1/factory/budget/reservations", GatewayRole.CAPTAIN),
        ("/v1/factory/budget/usage", GatewayRole.WORKER),
        ("/v1/factory/budget/releases", GatewayRole.CAPTAIN),
        ("/v1/factory/workflow-artifacts", GatewayRole.WORKER),
    ),
)
def test_factory_validation_errors_never_echo_rejected_secret_input(
    path: str,
    actor: GatewayRole,
    caplog,
) -> None:
    sentinel = "SECRET_SENTINEL_NEVER_ECHO_123456"

    with TestClient(validation_only_app(actor)) as client:
        response = client.post(path, json={"password": sentinel})

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid factory request"}
    assert sentinel not in response.text
    assert sentinel not in caplog.text


def test_usage_submission_adds_subject_and_active_lease_binding(job_v3) -> None:
    reservation = GatewayFactoryBudgetLedger(BudgetStore(job_v3)).reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )
    receipt = FactoryUsageReceiptV1.model_validate(usage_payload(reservation))

    submission = FactoryUsageSubmissionV2(
        subject_version=job_v3.subject_version,
        lease_id="lease-real-case-tester",
        receipt=receipt,
    )

    assert submission.receipt == receipt
    with pytest.raises(ValidationError):
        FactoryUsageSubmissionV2.model_validate(
            {"schema": "captain.factory-usage-submission.v2", "receipt": receipt}
        )


def test_gateway_usage_submission_requires_exact_active_ledger_lease(job_v3) -> None:
    budget_store = BudgetStore(job_v3)
    reservation = GatewayFactoryBudgetLedger(budget_store).reserve(
        job_v3, attempt=2, requested_usd=Decimal("1.00"), now=NOW
    )
    submission = FactoryUsageSubmissionV2(
        subject_version=job_v3.subject_version,
        lease_id=budget_store.lease.lease_id,
        receipt=FactoryUsageReceiptV1.model_validate(usage_payload(reservation)),
    )

    class Storage:
        @staticmethod
        def _decode_row(row):
            return row

    class Cursor:
        def __init__(self, lease) -> None:
            self.lease = lease

        def execute(self, _sql, _parameters) -> None:
            return None

        def fetchone(self):
            if self.lease is None:
                return None
            return {
                "data": self.lease.model_dump(mode="json", by_alias=True)
            }

    store = object.__new__(GatewayStore)
    store.storage = Storage()
    store._assert_budget_usage_lease(
        Cursor(budget_store.lease), job_v3, submission
    )
    wrong_effect_lease = issue_factory_lease(
        job=job_v3,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=2,
        workspace_ref="workspace://factory/budget-test",
        now=job_v3.occurred_at,
    )
    with pytest.raises(HTTPException, match="paid model effect"):
        store._assert_budget_usage_lease(
            Cursor(wrong_effect_lease),
            job_v3,
            submission.model_copy(update={"lease_id": wrong_effect_lease.lease_id}),
        )
    with pytest.raises(HTTPException, match="subject binding"):
        store._assert_budget_usage_lease(
            Cursor(budget_store.lease),
            job_v3,
            submission.model_copy(update={"subject_version": 2}),
        )
    with pytest.raises(HTTPException, match="active lease"):
        store._assert_budget_usage_lease(Cursor(None), job_v3, submission)

    assert GatewayStore._factory_usage_receipt(
        submission.receipt.model_dump(mode="json", by_alias=True)
    ) == submission.receipt
    assert GatewayStore._factory_usage_receipt(
        submission.model_dump(mode="json", by_alias=True)
    ) == submission.receipt


def test_gateway_budget_adapter_translates_http_conflicts_to_budget_domain_error(
    job_v3,
) -> None:
    class ExhaustedStore(BudgetStore):
        def reserve_factory_budget(self, request):
            raise HTTPException(status_code=409, detail="factory USD budget is exhausted")

    with pytest.raises(BudgetExhausted, match="exhausted"):
        GatewayFactoryBudgetLedger(ExhaustedStore(job_v3)).reserve(
            job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
        )


def test_factory_projection_preserves_versioned_job_schema_and_execution_policy(job_v3) -> None:
    projection = FactoryProjection.from_job(job_v3)

    assert projection.job == job_v3
    payload = projection.model_dump(mode="json", by_alias=True)
    assert payload["job"]["schema"] == "captain.agent-factory-job.v3"
    assert payload["job"]["execution_policy"] == job_v3.execution_policy.model_dump(
        mode="json", by_alias=True
    )

    historical_v2 = job_v2()
    v2_payload = FactoryProjection.from_job(historical_v2).model_dump(
        mode="json", by_alias=True
    )
    assert v2_payload["job"] == historical_v2.model_dump(
        mode="json", by_alias=True
    )


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

    terminal = FactoryProjection.from_job(job_v3).model_copy(
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


def record_usage_lease(store: GatewayStore, factory_job):
    def evidence(phase: FactoryPhase, lease=None):
        return block(phase).model_copy(
            update={
                "job_id": factory_job.job_id,
                "correlation_id": factory_job.correlation_id,
                "subject_version": factory_job.subject_version,
                "occurred_at": factory_job.occurred_at,
                "lease_id": lease.lease_id if lease is not None else None,
            }
        )

    store.record_factory_block(evidence(FactoryPhase.FORGE_REQUESTED))
    architect = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://factory/budget-test",
        now=factory_job.occurred_at,
    )
    store.record_factory_lease(architect)
    store.record_factory_block(evidence(FactoryPhase.BLUEPRINT_CREATED, architect))
    integrator = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://factory/budget-test",
        now=factory_job.occurred_at,
    )
    store.record_factory_lease(integrator)
    for phase in (
        FactoryPhase.TOOL_CANDIDATE_TESTED,
        FactoryPhase.AGENT_CODE_CREATED,
        FactoryPhase.BUILD_PASSED,
    ):
        store.record_factory_block(evidence(phase, integrator))
    real_case = issue_factory_lease(
        job=factory_job,
        role=FactoryRole.REAL_CASE_TESTER,
        attempt=1,
        workspace_ref="workspace://factory/budget-test",
        now=factory_job.occurred_at,
    )
    store.record_factory_lease(real_case)
    return real_case


def test_mariadb_budget_replay_conflict_and_restart_projection(
    mariadb_store: GatewayStore,
    job_v3,
) -> None:
    mariadb_store.record_factory_job(job_v3)
    lease = record_usage_lease(mariadb_store, job_v3)
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
    mariadb_store.record_factory_usage(
        FactoryUsageSubmissionV2(
            subject_version=job_v3.subject_version,
            lease_id=lease.lease_id,
            receipt=usage,
        )
    )
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
    lease = record_usage_lease(mariadb_store, job_v3)
    reservation = GatewayFactoryBudgetLedger(mariadb_store).reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )

    with pytest.raises(HTTPException, match="unapproved model"):
        mariadb_store.record_factory_usage(
            FactoryUsageSubmissionV2(
                subject_version=job_v3.subject_version,
                lease_id=lease.lease_id,
                receipt=FactoryUsageReceiptV1.model_validate(
                    usage_payload(reservation, model="unknown-model")
                ),
            )
        )
    with pytest.raises(HTTPException, match="exceeds"):
        mariadb_store.record_factory_usage(
            FactoryUsageSubmissionV2(
                subject_version=job_v3.subject_version,
                lease_id=lease.lease_id,
                receipt=FactoryUsageReceiptV1.model_validate(
                    usage_payload(
                        reservation,
                        receipt_id=UUID(
                            "20000000-0000-0000-0000-000000000002"
                        ),
                        cost_usd="1.01",
                    )
                ),
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
            "required_capability": artifact.invocation.released_skill.capability,
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
    mariadb_store.record_released_factory_skill(
        artifact.invocation.released_skill
    )

    fabricated_skill = artifact.invocation.released_skill.model_copy(
        update={"content_sha256": "f" * 64}
    )
    fabricated_invocation = artifact.invocation.model_copy(
        update={
            "invocation_id": UUID("00000000-0000-0000-0000-000000000399"),
            "released_skill": fabricated_skill,
        }
    )
    fabricated = artifact.model_copy(
        update={
            "invocation": fabricated_invocation,
            "invocation_id": fabricated_invocation.invocation_id,
        }
    )
    with pytest.raises(HTTPException, match="unknown released skill"):
        mariadb_store.record_factory_workflow_artifact(fabricated)

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
        except (HTTPException, BudgetExhausted) as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(reserve, (1, 2)))

    assert sum(isinstance(item, FactoryBudgetReservationV1) for item in outcomes) == 1
    assert sum(isinstance(item, BudgetExhausted) for item in outcomes) == 1
    assert mariadb_store.factory_budget(job_v3.job_id).remaining_usd == Decimal("2.00")
