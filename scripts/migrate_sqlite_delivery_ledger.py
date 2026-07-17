"""Read-only, idempotent import of the retired SQLite delivery ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Literal, Protocol, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field


class LegacyImportRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    legacy_record_id: str = Field(min_length=1)
    batch_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,31}$")
    record_type: Literal["todo", "event"]
    data: dict[str, Any]


class LegacyImportReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sqlite_path: str
    dry_run: bool
    discovered_todos: int
    discovered_events: int
    imported_records: int
    already_present_records: int


class LegacyGatewayClient(Protocol):
    def import_record(self, record: LegacyImportRecord) -> bool: ...


class HttpLegacyGatewayClient:
    def __init__(self, base_url: str, token: str, client: httpx.Client) -> None:
        if not base_url.strip():
            raise ValueError("gateway URL must not be empty")
        if not token:
            raise ValueError("captain gateway token must not be empty")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._client = client

    def import_record(self, record: LegacyImportRecord) -> bool:
        try:
            response = self._client.post(
                f"{self._base_url}/imports/legacy-delivery",
                headers={"Authorization": f"Bearer {self._token}"},
                json=record.model_dump(mode="json"),
            )
        except httpx.HTTPError:
            raise RuntimeError("legacy import could not reach the gateway") from None
        if response.status_code != 201:
            raise RuntimeError(
                f"legacy import failed with gateway status {response.status_code}"
            )
        payload = response.json()
        if not isinstance(payload, dict) or type(payload.get("created")) is not bool:
            raise RuntimeError("legacy import returned an invalid gateway response")
        return payload["created"]


def _batch_id(todo_id: str) -> str:
    digest = hashlib.sha256(todo_id.encode("utf-8")).hexdigest()[:24]
    return f"legacy-{digest}"


def _read_records(path: Path) -> tuple[list[LegacyImportRecord], int, int]:
    resolved = path.resolve()
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    uri = f"file:{resolved.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.row_factory = sqlite3.Row
        todos = connection.execute(
            "SELECT todo_id, document FROM delivery_todos ORDER BY rowid"
        ).fetchall()
        events = connection.execute(
            "SELECT sequence, event_id, todo_id, actor, event_type, payload, created_at "
            "FROM delivery_events ORDER BY sequence"
        ).fetchall()

    records: list[LegacyImportRecord] = []
    batch_ids: dict[str, str] = {}
    for row in todos:
        todo_id = str(row["todo_id"])
        batch_id = _batch_id(todo_id)
        batch_ids[todo_id] = batch_id
        records.append(
            LegacyImportRecord(
                legacy_record_id=f"todo:{todo_id}",
                batch_id=batch_id,
                record_type="todo",
                data={
                    "batch_id": batch_id,
                    "legacy_todo_id": todo_id,
                    "todo": json.loads(row["document"]),
                },
            )
        )
    for row in events:
        todo_id = str(row["todo_id"])
        if todo_id not in batch_ids:
            raise ValueError(f"legacy event references missing todo {todo_id!r}")
        event_id = str(row["event_id"])
        records.append(
            LegacyImportRecord(
                legacy_record_id=f"event:{event_id}",
                batch_id=batch_ids[todo_id],
                record_type="event",
                data={
                    "batch_id": batch_ids[todo_id],
                    "legacy_todo_id": todo_id,
                    "legacy_event_id": event_id,
                    "actor": row["actor"],
                    "event_type": row["event_type"],
                    "payload": json.loads(row["payload"]),
                    "created_at": row["created_at"],
                    "legacy_sequence": row["sequence"],
                },
            )
        )
    return records, len(todos), len(events)


def migrate(
    sqlite_path: Path | str,
    gateway_client: LegacyGatewayClient,
    *,
    dry_run: bool,
    confirm_import: bool,
) -> LegacyImportReport:
    path = Path(sqlite_path)
    records, todo_count, event_count = _read_records(path)
    if not dry_run and not confirm_import:
        raise ValueError("confirm_import is required for a non-dry migration")

    imported = 0
    already_present = 0
    if not dry_run:
        for record in records:
            if gateway_client.import_record(record):
                imported += 1
            else:
                already_present += 1
    return LegacyImportReport(
        sqlite_path=str(path.resolve()),
        dry_run=dry_run,
        discovered_todos=todo_count,
        discovered_events=event_count,
        imported_records=imported,
        already_present_records=already_present,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite-path", type=Path, required=True)
    parser.add_argument("--gateway-url", default="http://127.0.0.1:8000")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--confirm-import", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.dry_run:
        class DryRunClient:
            def import_record(self, record: LegacyImportRecord) -> bool:
                del record
                raise AssertionError("dry-run must not call the gateway")

        report = migrate(
            args.sqlite_path,
            DryRunClient(),
            dry_run=True,
            confirm_import=False,
        )
    else:
        token = os.getenv("CAPTAIN_GATEWAY_TOKEN")
        if not token:
            raise RuntimeError("CAPTAIN_GATEWAY_TOKEN is required for confirmed import")
        with httpx.Client() as http:
            report = migrate(
                args.sqlite_path,
                HttpLegacyGatewayClient(args.gateway_url, token, http),
                dry_run=False,
                confirm_import=True,
            )
    print(report.model_dump_json())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
