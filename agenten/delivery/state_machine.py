from __future__ import annotations

from .models import DeliveryRole, DeliveryStatus, DeliveryTodo


class DeliveryTransitionError(ValueError):
    pass


LEGAL_TARGETS: dict[DeliveryStatus, frozenset[DeliveryStatus]] = {
    DeliveryStatus.PLANNED: frozenset({DeliveryStatus.ASSIGNED}),
    DeliveryStatus.ASSIGNED: frozenset({DeliveryStatus.IN_PROGRESS}),
    DeliveryStatus.IN_PROGRESS: frozenset({DeliveryStatus.TESTING}),
    DeliveryStatus.TESTING: frozenset({DeliveryStatus.REVIEWING}),
    DeliveryStatus.REVIEWING: frozenset(
        {DeliveryStatus.PASSED, DeliveryStatus.REDO, DeliveryStatus.ESCALATED}
    ),
    DeliveryStatus.REDO: frozenset({DeliveryStatus.IN_PROGRESS}),
    DeliveryStatus.PASSED: frozenset(),
    DeliveryStatus.ESCALATED: frozenset(),
}


def validate_transition(
    todo: DeliveryTodo,
    actor: str,
    target: DeliveryStatus,
    assignee: DeliveryRole | None,
) -> None:
    effective_target = (
        DeliveryStatus.ESCALATED
        if todo.status is DeliveryStatus.REVIEWING
        and target is DeliveryStatus.REDO
        and todo.iteration >= todo.max_iterations
        else target
    )
    if effective_target not in LEGAL_TARGETS[todo.status]:
        raise DeliveryTransitionError(
            f"illegal transition {todo.status.value} -> {effective_target.value}"
        )
    if todo.status is DeliveryStatus.PLANNED:
        if actor != "captain" or assignee is None:
            raise DeliveryTransitionError("captain must assign an assigned role")
        return
    if todo.status in {DeliveryStatus.ASSIGNED, DeliveryStatus.REDO}:
        if todo.assignee is None or actor != todo.assignee.value:
            raise DeliveryTransitionError("only the assigned role may start work")
    elif todo.status is DeliveryStatus.IN_PROGRESS:
        if todo.assignee is None or actor != todo.assignee.value:
            raise DeliveryTransitionError("only the assigned role may submit testing")
    elif todo.status is DeliveryStatus.TESTING:
        if actor != DeliveryRole.REAL_CASE_TESTER.value:
            raise DeliveryTransitionError("real-case tester must open review")
    elif todo.status is DeliveryStatus.REVIEWING:
        if actor != DeliveryRole.QUALITY_WARDEN.value:
            raise DeliveryTransitionError("only the quality warden may decide review")


def resolved_target(
    todo: DeliveryTodo, target: DeliveryStatus
) -> DeliveryStatus:
    if (
        todo.status is DeliveryStatus.REVIEWING
        and target is DeliveryStatus.REDO
        and todo.iteration >= todo.max_iterations
    ):
        return DeliveryStatus.ESCALATED
    return target
