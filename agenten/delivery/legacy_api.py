from __future__ import annotations

from typing import Annotated

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict

from .events import DeliveryEventPublisher
from .ledger import SqliteDeliveryLedger
from .models import DeliveryEvent, DeliveryEvidence, DeliveryRole, DeliveryStatus, DeliveryTodo
from .state_machine import DeliveryTransitionError


class CreateTodoCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    title: str
    description: str
    acceptance_criteria: tuple[str, ...]
    dependencies: tuple[str, ...] = ()


class TransitionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    expected_version: int
    actor: str
    target: DeliveryStatus
    assignee: DeliveryRole | None = None


class AssignCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    expected_version: int
    actor: str
    assignee: DeliveryRole


class HeartbeatCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    expected_version: int
    actor: str


class EvidenceCommand(HeartbeatCommand):
    evidence: DeliveryEvidence


def create_legacy_delivery_app(
    ledger: SqliteDeliveryLedger,
    publisher: DeliveryEventPublisher | None = None,
) -> FastAPI:
    app = FastAPI(title="Legacy Captain Delivery Control Plane")

    def publish_latest(event_id: str) -> None:
        if publisher is None:
            return
        event = next(event for event in ledger.events_after(0) if event.event_id == event_id)
        publisher.publish(event)

    @app.post("/delivery/todos", response_model=DeliveryTodo, status_code=201)
    def create_todo(command: CreateTodoCommand) -> DeliveryTodo:
        todo = ledger.create_todo(**command.model_dump())
        publish_latest(f"todo-created:{todo.todo_id}")
        return todo

    @app.get("/delivery/todos/{todo_id}", response_model=DeliveryTodo)
    def get_todo(todo_id: str) -> DeliveryTodo:
        try:
            return ledger.get_todo(todo_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TODO not found") from error

    @app.get("/delivery/todos", response_model=list[DeliveryTodo])
    def list_todos(
        assignee: Annotated[DeliveryRole | None, Query()] = None,
        status: Annotated[DeliveryStatus | None, Query()] = None,
    ) -> list[DeliveryTodo]:
        return list(ledger.list_todos(assignee=assignee, status=status))

    @app.post("/delivery/todos/{todo_id}/transition", response_model=DeliveryTodo)
    def transition(todo_id: str, command: TransitionCommand) -> DeliveryTodo:
        existed = ledger.has_event(command.event_id)
        try:
            todo = ledger.transition(todo_id, **command.model_dump())
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TODO not found") from error
        except DeliveryTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not existed:
            publish_latest(command.event_id)
        return todo

    @app.post("/delivery/todos/{todo_id}/assign", response_model=DeliveryTodo)
    def assign(todo_id: str, command: AssignCommand) -> DeliveryTodo:
        return transition(
            todo_id,
            TransitionCommand(
                **command.model_dump(), target=DeliveryStatus.ASSIGNED
            ),
        )

    @app.post("/delivery/todos/{todo_id}/heartbeat", response_model=DeliveryTodo)
    def heartbeat(todo_id: str, command: HeartbeatCommand) -> DeliveryTodo:
        existed = ledger.has_event(command.event_id)
        try:
            todo = ledger.renew_lease(todo_id, **command.model_dump())
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TODO not found") from error
        except DeliveryTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not existed:
            publish_latest(command.event_id)
        return todo

    @app.post("/delivery/todos/{todo_id}/evidence", response_model=DeliveryTodo)
    def append_evidence(todo_id: str, command: EvidenceCommand) -> DeliveryTodo:
        existed = ledger.has_event(command.event_id)
        try:
            todo = ledger.append_evidence(
                todo_id=todo_id,
                event_id=command.event_id,
                expected_version=command.expected_version,
                actor=command.actor,
                evidence=command.evidence,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="TODO not found") from error
        except DeliveryTransitionError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if not existed:
            publish_latest(command.event_id)
        return todo

    @app.get("/delivery/events", response_model=list[DeliveryEvent])
    def events(after: Annotated[int, Query(ge=0)] = 0) -> list[DeliveryEvent]:
        return list(ledger.events_after(after))

    return app
