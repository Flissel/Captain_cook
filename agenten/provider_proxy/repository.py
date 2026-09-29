"""SQLite authority store for atomic provider-call reservations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


class ReservationRejected(ValueError):
    """The immutable run budget cannot admit another provider effect."""


@dataclass(frozen=True)
class BudgetProjection:
    call_count: int
    consumed_usd: Decimal
    active_reserved_usd: Decimal


class SQLiteProviderBudgetRepository:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS provider_call_reservations (
                    call_id TEXT PRIMARY KEY,
                    request_sha256 TEXT NOT NULL UNIQUE,
                    reserved_usd TEXT NOT NULL,
                    consumed_usd TEXT,
                    state TEXT NOT NULL CHECK (state IN ('reserved','settled','ambiguous')),
                    provider_response_id TEXT UNIQUE,
                    started_at TEXT
                );
                CREATE TABLE IF NOT EXISTS provider_cancellations (
                    provider_response_id TEXT PRIMARY KEY,
                    status_code INTEGER NOT NULL,
                    content_type TEXT NOT NULL,
                    body BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_call_usage (
                    call_id TEXT PRIMARY KEY,
                    provider_response_id TEXT NOT NULL UNIQUE,
                    input_units INTEGER NOT NULL,
                    cached_input_units INTEGER NOT NULL,
                    output_units INTEGER NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    FOREIGN KEY(call_id) REFERENCES provider_call_reservations(call_id)
                );
                CREATE TABLE IF NOT EXISTS provider_finalization (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    binding_sha256 TEXT NOT NULL,
                    settlement_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','settled'))
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def reserve(
        self,
        *,
        call_id: str,
        request_sha256: str,
        requested_usd: Decimal,
        ceiling_usd: Decimal,
        maximum_calls: int,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT reserved_usd, consumed_usd, state FROM provider_call_reservations"
            ).fetchall()
            count = len(rows)
            booked = sum(
                (
                    Decimal(str(row[0]))
                    if row[2] == "reserved"
                    else Decimal(str(row[1]))
                    for row in rows
                ),
                Decimal("0"),
            )
            if count >= maximum_calls:
                connection.rollback()
                raise ReservationRejected("provider call limit is exhausted")
            if requested_usd <= 0 or booked + requested_usd > ceiling_usd:
                connection.rollback()
                raise ReservationRejected("provider run budget is exhausted")
            try:
                connection.execute(
                    """INSERT INTO provider_call_reservations
                    (call_id, request_sha256, reserved_usd, state)
                    VALUES (?, ?, ?, 'reserved')""",
                    (call_id, request_sha256, format(requested_usd, "f")),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise ReservationRejected("provider request identity was already used") from exc
            connection.commit()

    def settle(
        self,
        *,
        call_id: str,
        provider_response_id: str,
        consumed_usd: Decimal,
        ambiguous: bool,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT reserved_usd, state, consumed_usd, provider_response_id FROM provider_call_reservations WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ReservationRejected("provider reservation is unavailable")
            reserved = Decimal(str(row[0]))
            effective = reserved if ambiguous else consumed_usd
            if effective < 0 or effective > reserved:
                connection.rollback()
                raise ReservationRejected("provider settlement exceeds its reservation")
            state = "ambiguous" if ambiguous else "settled"
            if row[1] != "reserved":
                if row[1:] == (state, format(effective, "f"), provider_response_id):
                    connection.commit()
                    return
                connection.rollback()
                raise ReservationRejected("provider reservation already changed")
            connection.execute(
                "UPDATE provider_call_reservations SET state=?, consumed_usd=?, provider_response_id=? WHERE call_id=?",
                (state, format(effective, "f"), provider_response_id, call_id),
            )
            connection.commit()

    def attach_response(self, *, call_id: str, response_id: str, started_at: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE provider_call_reservations SET provider_response_id=?, started_at=? WHERE call_id=? AND state='reserved' AND provider_response_id IS NULL",
                (response_id, started_at, call_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                raise ReservationRejected("provider response binding is unavailable")
            connection.commit()

    def active_response(self, response_id: str) -> tuple[str, Decimal, str] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT call_id, reserved_usd, started_at FROM provider_call_reservations WHERE provider_response_id=? AND state='reserved'",
                (response_id,),
            ).fetchone()
        if row is None or row[2] is None:
            return None
        return str(row[0]), Decimal(str(row[1])), str(row[2])

    def projection(self) -> BudgetProjection:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT reserved_usd, consumed_usd, state FROM provider_call_reservations"
            ).fetchall()
        consumed = sum(
            (Decimal(row[1]) for row in rows if row[1] is not None), Decimal("0")
        )
        active = sum(
            (Decimal(row[0]) for row in rows if row[2] == "reserved"), Decimal("0")
        )
        return BudgetProjection(len(rows), consumed, active)

    def record_usage(
        self,
        *,
        call_id: str,
        provider_response_id: str,
        consumed_usd: Decimal,
        input_units: int,
        cached_input_units: int,
        output_units: int,
        started_at: str,
        ended_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            reservation = connection.execute(
                "SELECT reserved_usd,state,consumed_usd,provider_response_id FROM provider_call_reservations WHERE call_id=?",
                (call_id,),
            ).fetchone()
            if reservation is None or consumed_usd > Decimal(str(reservation[0])):
                connection.rollback()
                raise ReservationRejected("provider usage exceeds its reservation")
            expected_usage = (
                provider_response_id,
                input_units,
                cached_input_units,
                output_units,
                started_at,
                ended_at,
            )
            existing = connection.execute(
                "SELECT provider_response_id,input_units,cached_input_units,output_units,started_at,ended_at FROM provider_call_usage WHERE call_id=?",
                (call_id,),
            ).fetchone()
            if reservation[1] != "reserved":
                if (
                    reservation[1] == "settled"
                    and Decimal(str(reservation[2])) == consumed_usd
                    and reservation[3] == provider_response_id
                    and existing == expected_usage
                ):
                    connection.commit()
                    return
                connection.rollback()
                raise ReservationRejected("provider usage receipt already changed")
            try:
                connection.execute(
                    "UPDATE provider_call_reservations SET state='settled', consumed_usd=?, provider_response_id=? WHERE call_id=?",
                    (format(consumed_usd, "f"), provider_response_id, call_id),
                )
                connection.execute(
                    "INSERT INTO provider_call_usage VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        call_id,
                        provider_response_id,
                        input_units,
                        cached_input_units,
                        output_units,
                        started_at,
                        ended_at,
                    ),
                )
            except sqlite3.IntegrityError:
                connection.rollback()
                raise ReservationRejected("provider usage receipt already changed") from None
            connection.commit()

    def aggregate_usage(self) -> tuple[int, int, int, str, str, tuple[str, ...]]:
        projection = self.projection()
        if projection.active_reserved_usd > 0:
            raise ReservationRejected("provider calls are not terminal")
        with self._connect() as connection:
            ambiguous = connection.execute(
                "SELECT COUNT(*) FROM provider_call_reservations WHERE state='ambiguous'"
            ).fetchone()
            rows = connection.execute(
                "SELECT provider_response_id,input_units,cached_input_units,output_units,started_at,ended_at FROM provider_call_usage ORDER BY call_id"
            ).fetchall()
        if ambiguous is None or int(ambiguous[0]) > 0 or len(rows) != projection.call_count:
            raise ReservationRejected("provider usage is ambiguous or incomplete")
        if not rows:
            raise ReservationRejected("provider usage is unavailable")
        return (
            sum(int(row[1]) for row in rows),
            sum(int(row[2]) for row in rows),
            sum(int(row[3]) for row in rows),
            min(str(row[4]) for row in rows),
            max(str(row[5]) for row in rows),
            tuple(str(row[0]) for row in rows),
        )

    def claim_finalization(self, binding_sha256: str, settlement_json: str) -> str:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT binding_sha256, settlement_json, state FROM provider_finalization WHERE singleton=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO provider_finalization VALUES (1, ?, ?, 'pending')",
                    (binding_sha256, settlement_json),
                )
                connection.commit()
                return "pending"
            if row[0] != binding_sha256 or row[1] != settlement_json:
                connection.rollback()
                raise ReservationRejected("provider finalization binding changed")
            connection.commit()
            return str(row[2])

    def complete_finalization(self, binding_sha256: str) -> None:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE provider_finalization SET state='settled' WHERE singleton=1 AND binding_sha256=?",
                (binding_sha256,),
            ).rowcount
            if changed != 1:
                raise ReservationRejected("provider finalization is unavailable")

    def cancellation(self, response_id: str) -> tuple[int, str, bytes] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status_code, content_type, body FROM provider_cancellations WHERE provider_response_id=?",
                (response_id,),
            ).fetchone()
        return None if row is None else (int(row[0]), str(row[1]), bytes(row[2]))

    def record_cancellation(
        self, response_id: str, status_code: int, content_type: str, body: bytes
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO provider_cancellations VALUES (?, ?, ?, ?)",
                (response_id, status_code, content_type, body),
            )
