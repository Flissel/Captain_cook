from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from agenten.delivery.ledger import SqliteDeliveryLedger
from agenten.delivery.models import DeliveryRole, DeliveryStatus
from scripts.migrate_sqlite_delivery_ledger import (
    LegacyImportRecord,
    migrate,
)


class RecordingGatewayClient:
    def __init__(self) -> None:
        self.records: dict[str, LegacyImportRecord] = {}
        self.calls: list[LegacyImportRecord] = []

    def import_record(self, record: LegacyImportRecord) -> bool:
        self.calls.append(record)
        existing = self.records.get(record.legacy_record_id)
        if existing is not None:
            assert existing == record
            return False
        self.records[record.legacy_record_id] = record
        return True


def seeded_legacy_ledger(tmp_path: Path) -> Path:
    path = tmp_path / "delivery.db"
    ledger = SqliteDeliveryLedger(path)
    todo = ledger.create_todo(
        "legacy-project",
        "Ship webhook",
        "Build and verify the webhook",
        ("HTTP 200",),
        todo_id="todo-1",
        event_id="event-created",
    )
    ledger.transition(
        todo.todo_id,
        "event-assigned",
        todo.version,
        "captain",
        DeliveryStatus.ASSIGNED,
        assignee=DeliveryRole.ARCHITECT_BUILDER,
    )
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migration_dry_run_is_read_only_and_sends_nothing(tmp_path: Path) -> None:
    sqlite_path = seeded_legacy_ledger(tmp_path)
    before = sha256(sqlite_path)
    gateway = RecordingGatewayClient()

    report = migrate(sqlite_path, gateway, dry_run=True, confirm_import=False)

    assert report.discovered_todos == 1
    assert report.discovered_events == 2
    assert report.imported_records == 0
    assert gateway.calls == []
    assert sha256(sqlite_path) == before


def test_non_dry_migration_requires_confirmation(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="confirm_import"):
        migrate(
            seeded_legacy_ledger(tmp_path),
            RecordingGatewayClient(),
            dry_run=False,
            confirm_import=False,
        )


def test_migration_replay_is_idempotent(tmp_path: Path) -> None:
    sqlite_path = seeded_legacy_ledger(tmp_path)
    gateway = RecordingGatewayClient()

    first = migrate(sqlite_path, gateway, dry_run=False, confirm_import=True)
    second = migrate(sqlite_path, gateway, dry_run=False, confirm_import=True)

    assert first.imported_records == 3
    assert first.already_present_records == 0
    assert second.imported_records == 0
    assert second.already_present_records == 3
    assert [record.record_type for record in gateway.calls[:3]] == ["todo", "event", "event"]
    assert gateway.calls[1].data["legacy_event_id"] == "event-created"
    assert gateway.calls[2].data["legacy_event_id"] == "event-assigned"


def test_distinct_legacy_ids_cannot_collapse_to_the_same_batch_id(tmp_path: Path) -> None:
    sqlite_path = seeded_legacy_ledger(tmp_path)
    SqliteDeliveryLedger(sqlite_path).create_todo(
        "legacy-project",
        "Ship second webhook",
        "Keep punctuation-distinct identifiers separate",
        ("HTTP 200",),
        todo_id="todo1",
        event_id="event-created-2",
    )
    gateway = RecordingGatewayClient()

    migrate(sqlite_path, gateway, dry_run=False, confirm_import=True)

    todo_batch_ids = {
        record.batch_id for record in gateway.calls if record.record_type == "todo"
    }
    assert len(todo_batch_ids) == 2
