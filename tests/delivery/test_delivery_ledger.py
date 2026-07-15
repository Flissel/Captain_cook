from pathlib import Path

import pytest

from agenten.delivery import (
    DeliveryRole,
    DeliveryStatus,
    DeliveryTransitionError,
    SqliteDeliveryLedger,
)


@pytest.fixture
def real_ledger(tmp_path: Path) -> SqliteDeliveryLedger:
    return SqliteDeliveryLedger(tmp_path / "delivery.db")


def _planned(ledger: SqliteDeliveryLedger):
    return ledger.create_todo(
        project_id="captain-cook",
        title="Build real webhook",
        description="Deploy and execute the webhook",
        acceptance_criteria=("HTTP 200", "correlated Mailpit message"),
    )


def _assigned(ledger: SqliteDeliveryLedger):
    todo = _planned(ledger)
    return ledger.transition(
        todo.todo_id,
        event_id="assign-1",
        expected_version=todo.version,
        actor="captain",
        target=DeliveryStatus.ASSIGNED,
        assignee=DeliveryRole.ARCHITECT_BUILDER,
    )


def test_sqlite_ledger_persists_todo_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "delivery.db"
    first = SqliteDeliveryLedger(path)
    todo = _planned(first)

    second = SqliteDeliveryLedger(path)
    loaded = second.get_todo(todo.todo_id)

    assert loaded == todo
    assert loaded.status is DeliveryStatus.PLANNED
    assert loaded.iteration == 1
    assert loaded.max_iterations == 5


def test_only_expected_role_can_advance_working_state(
    real_ledger: SqliteDeliveryLedger,
) -> None:
    assigned = _assigned(real_ledger)

    with pytest.raises(DeliveryTransitionError, match="assigned role"):
        real_ledger.transition(
            assigned.todo_id,
            event_id="start-1",
            expected_version=assigned.version,
            actor=DeliveryRole.REAL_CASE_TESTER.value,
            target=DeliveryStatus.IN_PROGRESS,
        )


def test_duplicate_event_is_idempotent(real_ledger: SqliteDeliveryLedger) -> None:
    assigned = _assigned(real_ledger)
    first = real_ledger.transition(
        assigned.todo_id,
        event_id="same-event",
        expected_version=assigned.version,
        actor=DeliveryRole.ARCHITECT_BUILDER.value,
        target=DeliveryStatus.IN_PROGRESS,
    )
    second = real_ledger.transition(
        assigned.todo_id,
        event_id="same-event",
        expected_version=assigned.version,
        actor=DeliveryRole.ARCHITECT_BUILDER.value,
        target=DeliveryStatus.IN_PROGRESS,
    )

    assert second == first
    assert len(real_ledger.events_after(0)) == 3


def test_review_rejection_increments_iteration_then_escalates_at_five(
    real_ledger: SqliteDeliveryLedger,
) -> None:
    todo = _assigned(real_ledger)
    todo = real_ledger.transition(
        todo.todo_id, "start", todo.version,
        DeliveryRole.ARCHITECT_BUILDER.value, DeliveryStatus.IN_PROGRESS,
    )
    for iteration in range(1, 6):
        todo = real_ledger.transition(
            todo.todo_id, f"test-{iteration}", todo.version,
            DeliveryRole.ARCHITECT_BUILDER.value, DeliveryStatus.TESTING,
        )
        todo = real_ledger.transition(
            todo.todo_id, f"review-{iteration}", todo.version,
            DeliveryRole.REAL_CASE_TESTER.value, DeliveryStatus.REVIEWING,
        )
        todo = real_ledger.transition(
            todo.todo_id, f"reject-{iteration}", todo.version,
            DeliveryRole.QUALITY_WARDEN.value, DeliveryStatus.REDO,
        )
        if iteration < 5:
            assert todo.iteration == iteration + 1
            todo = real_ledger.transition(
                todo.todo_id, f"restart-{iteration}", todo.version,
                DeliveryRole.ARCHITECT_BUILDER.value, DeliveryStatus.IN_PROGRESS,
            )

    assert todo.status is DeliveryStatus.ESCALATED
    assert todo.iteration == 5


def test_builder_cannot_mark_work_passed(real_ledger: SqliteDeliveryLedger) -> None:
    todo = _assigned(real_ledger)
    todo = real_ledger.transition(
        todo.todo_id, "start", todo.version,
        DeliveryRole.ARCHITECT_BUILDER.value, DeliveryStatus.IN_PROGRESS,
    )
    todo = real_ledger.transition(
        todo.todo_id, "testing", todo.version,
        DeliveryRole.ARCHITECT_BUILDER.value, DeliveryStatus.TESTING,
    )
    todo = real_ledger.transition(
        todo.todo_id, "review", todo.version,
        DeliveryRole.REAL_CASE_TESTER.value, DeliveryStatus.REVIEWING,
    )

    with pytest.raises(DeliveryTransitionError, match="quality warden"):
        real_ledger.transition(
            todo.todo_id, "pass", todo.version,
            DeliveryRole.ARCHITECT_BUILDER.value, DeliveryStatus.PASSED,
        )
