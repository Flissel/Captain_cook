from pathlib import Path

from fastapi.testclient import TestClient

from agenten.delivery import DeliveryStatus, SqliteDeliveryLedger
from agenten.delivery.api import create_delivery_app
from agenten.delivery.events import DeliveryEventPublisher
from agenten.runtime.event_bus import InMemoryEventBus


def test_api_runs_complete_delivery_lifecycle_on_real_sqlite(tmp_path: Path) -> None:
    ledger = SqliteDeliveryLedger(tmp_path / "delivery.db")
    app = create_delivery_app(ledger)
    client = TestClient(app)

    response = client.post(
        "/delivery/todos",
        json={
            "project_id": "captain-cook",
            "title": "Ship webhook",
            "description": "Build and prove it live",
            "acceptance_criteria": ["HTTP 200", "Mailpit correlation"],
        },
    )
    assert response.status_code == 201
    todo = response.json()

    commands = (
        ("assign", "captain", "assigned", {"assignee": "architect_builder"}),
        ("start", "architect_builder", "in_progress", {}),
        ("testing", "architect_builder", "testing", {}),
        ("review", "real_case_tester", "reviewing", {}),
        ("pass", "quality_warden", "passed", {}),
    )
    for event_id, actor, target, extra in commands:
        response = client.post(
            f"/delivery/todos/{todo['todo_id']}/transition",
            json={
                "event_id": event_id,
                "expected_version": todo["version"],
                "actor": actor,
                "target": target,
                **extra,
            },
        )
        assert response.status_code == 200, response.text
        todo = response.json()

    assert todo["status"] == "passed"
    stale = client.post(
        f"/delivery/todos/{todo['todo_id']}/transition",
        json={
            "event_id": "stale",
            "expected_version": 1,
            "actor": "captain",
            "target": "assigned",
            "assignee": "architect_builder",
        },
    )
    assert stale.status_code == 409


def test_publisher_observes_committed_state_and_duplicate_is_not_republished(
    tmp_path: Path,
) -> None:
    ledger = SqliteDeliveryLedger(tmp_path / "delivery.db")
    bus = InMemoryEventBus()
    observed_versions: list[int] = []

    async def reload_on_delivery(event: object) -> None:
        todo_id = getattr(event, "todo_id")
        observed_versions.append(ledger.get_todo(todo_id).version)

    bus.subscribe("delivery.events", reload_on_delivery)
    app = create_delivery_app(ledger, DeliveryEventPublisher(bus))
    client = TestClient(app)
    todo = client.post(
        "/delivery/todos",
        json={
            "project_id": "captain-cook",
            "title": "Persist first",
            "description": "Then publish",
            "acceptance_criteria": ["subscriber reload succeeds"],
        },
    ).json()
    command = {
        "event_id": "assign-once",
        "expected_version": todo["version"],
        "actor": "captain",
        "target": "assigned",
        "assignee": "architect_builder",
    }

    first = client.post(f"/delivery/todos/{todo['todo_id']}/transition", json=command)
    second = client.post(f"/delivery/todos/{todo['todo_id']}/transition", json=command)

    assert first.json() == second.json()
    assert observed_versions == [1, 2]
    assert len([e for e in ledger.events_after(0) if e.event_id == "assign-once"]) == 1


def test_operational_endpoints_persist_lease_evidence_and_events(tmp_path: Path) -> None:
    ledger = SqliteDeliveryLedger(tmp_path / "delivery.db")
    client = TestClient(create_delivery_app(ledger))
    todo = client.post(
        "/delivery/todos",
        json={
            "project_id": "captain-cook",
            "title": "Operational contract",
            "description": "Exercise real control-plane persistence",
            "acceptance_criteria": ["evidence is durable"],
        },
    ).json()
    assigned = client.post(
        f"/delivery/todos/{todo['todo_id']}/assign",
        json={
            "event_id": "assign-operational",
            "expected_version": todo["version"],
            "actor": "captain",
            "assignee": "architect_builder",
        },
    ).json()
    heartbeat = client.post(
        f"/delivery/todos/{todo['todo_id']}/heartbeat",
        json={
            "event_id": "heartbeat-1",
            "expected_version": assigned["version"],
            "actor": "architect_builder",
        },
    )
    assert heartbeat.status_code == 200, heartbeat.text
    renewed = heartbeat.json()
    assert renewed["lease_expires_at"] is not None

    evidence = client.post(
        f"/delivery/todos/{todo['todo_id']}/evidence",
        json={
            "event_id": "evidence-1",
            "expected_version": renewed["version"],
            "actor": "architect_builder",
            "evidence": {
                "kind": "command_transcript",
                "uri": "artifacts/run-1.json",
                "sha256": "a" * 64,
            },
        },
    )
    assert evidence.status_code == 200, evidence.text
    assert evidence.json()["evidence"][0]["sha256"] == "a" * 64

    events = client.get("/delivery/events", params={"after": 1})
    assert events.status_code == 200
    assert all(event["sequence"] > 1 for event in events.json())
