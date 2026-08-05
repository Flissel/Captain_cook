from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from agenten.agent_factory.contracts import AgentFactoryJobV3
from agenten.agent_factory.execution_budget import (
    BudgetExhausted,
    FactoryBudgetProjection,
    FactoryBudgetReservationV1,
    FactoryBudgetWriteReceipt,
    FactoryUsageReceiptV1,
    InMemoryFactoryBudgetLedger,
)


NOW = datetime(2026, 7, 21, 12, tzinfo=timezone.utc)


def test_budget_projection_preserves_micro_usd_provider_usage() -> None:
    projection = FactoryBudgetProjection(
        job_id=UUID("10000000-0000-0000-0000-000000000003"),
        limit_usd="0.40",
        consumed_usd="0.130133",
        reserved_usd="0.00",
        remaining_usd="0.269867",
    )

    assert projection.consumed_usd == Decimal("0.130133")
    assert projection.remaining_usd == Decimal("0.269867")


def artifact(kind: str, digest: str) -> dict[str, str]:
    return {
        "uri": f"artifact://{kind}/{digest}",
        "sha256": digest,
        "media_type": "application/json",
    }


@pytest.fixture
def job_v3() -> AgentFactoryJobV3:
    return AgentFactoryJobV3.model_validate(
        {
            "schema": "captain.agent-factory-job.v3",
            "event_id": "10000000-0000-0000-0000-000000000001",
            "correlation_id": "10000000-0000-0000-0000-000000000002",
            "causation_id": None,
            "occurred_at": NOW,
            "producer": "captain",
            "job_id": "10000000-0000-0000-0000-000000000003",
            "subject_version": 1,
            "input_ref": artifact("factory-input", "a" * 64)
            | {"media_type": "text/markdown"},
            "compiled_spec_ref": artifact("compiled-factory-spec", "b" * 64),
            "dependency_graph_ref": artifact("factory-work-graph", "c" * 64),
            "required_capability": "customer_support_triage",
            "acceptance_assertion_ids": ["assert-111111111111"],
            "private_holdout_refs": [
                {
                    "schema_name": "captain.private-holdout-ref.v1",
                    "holdout_id": "holdout-222222222222",
                    "uri": "holdout://holdout-222222222222",
                    "sha256": "d" * 64,
                }
            ],
            "max_behavioral_iterations": 5,
            "deadline_at": NOW + timedelta(minutes=15),
            "execution_policy": {
                "schema": "captain.factory-execution-policy.v1",
                "mode": "release",
                "live_execution": True,
                "max_cost_usd": "5.00",
                "max_runtime_seconds": 900,
                "required_live_runs": 3,
                "allowed_models": ["approved-model-id"],
                "live_capabilities": ["model.invoke"],
                "sandbox_mode": "workspace_write",
            },
        }
    )


def usage_payload(
    reservation: FactoryBudgetReservationV1,
    *,
    cost_usd: object = "1.25",
    receipt_id: UUID | None = None,
    model: str = "approved-model-id",
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "schema": "captain.factory-usage-receipt.v1",
        "receipt_id": str(receipt_id or UUID("20000000-0000-0000-0000-000000000001")),
        "reservation_id": str(reservation.reservation_id),
        "job_id": str(reservation.job_id),
        "correlation_id": str(reservation.correlation_id),
        "attempt": reservation.attempt,
        "provider": "approved-provider",
        "model": model,
        "input_units": 100,
        "output_units": 25,
        "cost_usd": cost_usd,
        "started_at": started_at or reservation.reserved_at,
        "ended_at": ended_at or reservation.reserved_at + timedelta(seconds=1),
        "evidence_ref": artifact("factory-usage", "e" * 64),
    }


def test_reservation_prevents_concurrent_overspend(job_v3: AgentFactoryJobV3) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    barrier = Barrier(2)

    def reserve() -> FactoryBudgetReservationV1 | BudgetExhausted:
        barrier.wait()
        try:
            return ledger.reserve(
                job_v3, attempt=1, requested_usd=Decimal("3.00"), now=NOW
            )
        except BudgetExhausted as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = tuple(executor.map(lambda _: reserve(), range(2)))

    reservations = tuple(
        item for item in outcomes if isinstance(item, FactoryBudgetReservationV1)
    )
    exhausted = tuple(item for item in outcomes if isinstance(item, BudgetExhausted))
    assert len(reservations) == 1
    assert len(exhausted) == 1
    projection = ledger.projection(job_v3.job_id)
    assert projection.reserved_usd == Decimal("3.00")
    assert projection.remaining_usd == Decimal("2.00")


def test_budget_is_cumulative_across_attempts(job_v3: AgentFactoryJobV3) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    ledger.reserve(job_v3, attempt=1, requested_usd=Decimal("3.00"), now=NOW)
    ledger.reserve(job_v3, attempt=2, requested_usd=Decimal("2.00"), now=NOW)

    with pytest.raises(BudgetExhausted):
        ledger.reserve(job_v3, attempt=3, requested_usd=Decimal("0.01"), now=NOW)


def test_known_usage_consumes_reservation_and_replay_is_idempotent(
    job_v3: AgentFactoryJobV3,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )
    receipt = FactoryUsageReceiptV1.model_validate(usage_payload(reservation))

    first = ledger.record_usage(job_v3, reservation, receipt)
    replay = ledger.record_usage(job_v3, reservation, receipt)

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.event_id == first.event_id
    projection = ledger.projection(job_v3.job_id)
    assert projection.consumed_usd == Decimal("1.25")
    assert projection.reserved_usd == Decimal("0.00")
    assert projection.remaining_usd == Decimal("3.75")


def test_unknown_or_non_decimal_usage_never_counts_as_success(
    job_v3: AgentFactoryJobV3,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )
    with pytest.raises(ValidationError, match="known USD cost"):
        FactoryUsageReceiptV1.model_validate(
            usage_payload(reservation, cost_usd=None)
        )
    with pytest.raises(ValidationError, match="decimal string"):
        FactoryUsageReceiptV1.model_validate(
            usage_payload(reservation, cost_usd=0.5)
        )


def test_decimal_receipts_are_semantically_canonical(
    job_v3: AgentFactoryJobV3,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )
    one_decimal = FactoryUsageReceiptV1.model_validate(
        usage_payload(reservation, cost_usd="1.0")
    )
    two_decimals = FactoryUsageReceiptV1.model_validate(
        usage_payload(reservation, cost_usd="1.00")
    )

    assert one_decimal.model_dump(mode="json", by_alias=True) == two_decimals.model_dump(
        mode="json", by_alias=True
    )


def test_changed_receipt_replay_conflicts(job_v3: AgentFactoryJobV3) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )
    receipt = FactoryUsageReceiptV1.model_validate(usage_payload(reservation))
    ledger.record_usage(job_v3, reservation, receipt)
    changed = FactoryUsageReceiptV1.model_validate(
        usage_payload(reservation, cost_usd="1.50", receipt_id=receipt.receipt_id)
    )

    with pytest.raises(ValueError, match="different content"):
        ledger.record_usage(job_v3, reservation, changed)


def test_receipt_replay_cannot_change_job_budget_identity(
    job_v3: AgentFactoryJobV3,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )
    receipt = FactoryUsageReceiptV1.model_validate(usage_payload(reservation))
    ledger.record_usage(job_v3, reservation, receipt)
    lower_policy = job_v3.execution_policy.model_copy(
        update={"max_cost_usd": Decimal("2.00")}
    )
    changed_job = job_v3.model_copy(update={"execution_policy": lower_policy})

    with pytest.raises(ValueError, match="identity"):
        ledger.record_usage(changed_job, reservation, receipt)


def test_usage_rejects_changed_policy_under_unchanged_job_identity(
    job_v3: AgentFactoryJobV3,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )
    expanded_policy = job_v3.execution_policy.model_copy(
        update={
            "allowed_models": (
                "approved-model-id",
                "unreleased-model-id",
            )
        }
    )
    changed_job = job_v3.model_copy(update={"execution_policy": expanded_policy})
    receipt = FactoryUsageReceiptV1.model_validate(
        usage_payload(reservation, model="unreleased-model-id")
    )

    with pytest.raises(ValueError, match="execution policy"):
        ledger.record_usage(changed_job, reservation, receipt)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"model": "unapproved-model"}, "allowed model"),
        ({"cost_usd": "2.01"}, "reservation"),
    ],
)
def test_usage_fails_closed_outside_model_or_cost_authority(
    job_v3: AgentFactoryJobV3,
    overrides: dict[str, object],
    message: str,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )
    receipt = FactoryUsageReceiptV1.model_validate(
        usage_payload(reservation, **overrides)
    )

    with pytest.raises(BudgetExhausted, match=message):
        ledger.record_usage(job_v3, reservation, receipt)


def test_usage_must_fit_reservation_time_window(job_v3: AgentFactoryJobV3) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )
    too_late = FactoryUsageReceiptV1.model_validate(
        usage_payload(
            reservation,
            started_at=reservation.expires_at - timedelta(seconds=1),
            ended_at=reservation.expires_at + timedelta(seconds=1),
        )
    )

    with pytest.raises(ValueError, match="window"):
        ledger.record_usage(job_v3, reservation, too_late)


def test_usage_requires_a_preexisting_active_reservation(
    job_v3: AgentFactoryJobV3,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    issued_elsewhere = InMemoryFactoryBudgetLedger().reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )
    receipt = FactoryUsageReceiptV1.model_validate(usage_payload(issued_elsewhere))

    with pytest.raises(ValueError, match="not issued"):
        ledger.record_usage(job_v3, issued_elsewhere, receipt)


def test_release_is_idempotent_and_returns_unused_budget(
    job_v3: AgentFactoryJobV3,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    reservation = ledger.reserve(
        job_v3, attempt=1, requested_usd=Decimal("2.00"), now=NOW
    )

    first = ledger.release(job_v3, reservation, now=NOW, reason="unused")
    replay = ledger.release(job_v3, reservation, now=NOW, reason="unused")

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.event_id == first.event_id
    projection = ledger.projection(job_v3.job_id)
    assert projection.reserved_usd == Decimal("0.00")
    assert projection.remaining_usd == Decimal("5.00")
    with pytest.raises(ValueError, match="different reason"):
        ledger.release(job_v3, reservation, now=NOW, reason="cancelled")


def test_reservation_rejects_invalid_amount_or_expired_job(
    job_v3: AgentFactoryJobV3,
) -> None:
    ledger = InMemoryFactoryBudgetLedger()
    for invalid in (Decimal("0.00"), Decimal("-1.00"), Decimal("0.001"), 1.0):
        with pytest.raises((TypeError, ValueError, BudgetExhausted)):
            ledger.reserve(job_v3, attempt=1, requested_usd=invalid, now=NOW)  # type: ignore[arg-type]
    with pytest.raises(BudgetExhausted, match="deadline"):
        ledger.reserve(
            job_v3,
            attempt=1,
            requested_usd=Decimal("1.00"),
            now=job_v3.deadline_at,
        )


def test_receipt_contract_is_strict_and_requires_ordered_utc_times(
    job_v3: AgentFactoryJobV3,
) -> None:
    reservation = InMemoryFactoryBudgetLedger().reserve(
        job_v3, attempt=1, requested_usd=Decimal("1.00"), now=NOW
    )
    with pytest.raises(ValidationError):
        FactoryUsageReceiptV1.model_validate(
            usage_payload(reservation) | {"unknown": True}
        )
    with pytest.raises(ValidationError, match="provider"):
        FactoryUsageReceiptV1.model_validate(
            usage_payload(reservation) | {"provider": "   "}
        )
    with pytest.raises(ValidationError):
        FactoryUsageReceiptV1.model_validate(
            usage_payload(reservation) | {"input_units": -1}
        )
    with pytest.raises(ValidationError, match="ended_at"):
        FactoryUsageReceiptV1.model_validate(
            usage_payload(
                reservation,
                started_at=NOW + timedelta(seconds=2),
                ended_at=NOW + timedelta(seconds=1),
            )
        )
    with pytest.raises(ValidationError, match="UTC"):
        FactoryUsageReceiptV1.model_validate(
            usage_payload(reservation) | {"started_at": datetime(2026, 7, 21, 12)}
        )


def test_write_receipt_replay_flag_is_strict(job_v3: AgentFactoryJobV3) -> None:
    with pytest.raises(ValidationError):
        FactoryBudgetWriteReceipt.model_validate(
            {
                "event_id": "30000000-0000-0000-0000-000000000001",
                "job_id": str(job_v3.job_id),
                "replayed": 1,
            }
        )


def test_projection_rejects_unknown_job() -> None:
    with pytest.raises(KeyError, match="unknown"):
        InMemoryFactoryBudgetLedger().projection(uuid4())
