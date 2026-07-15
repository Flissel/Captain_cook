"""Transactional MariaDB persistence for the Captain Cook ledger."""

from __future__ import annotations

import json
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

import pymysql
from pymysql.connections import Connection
from pymysql.cursors import DictCursor

from .storage import LedgerStorage


class MariaDBStorage(LedgerStorage):
    """Persist ledger blocks transactionally in a MariaDB ``blocks`` table."""

    def __init__(self, dsn: str):
        self._connection_options = self._parse_dsn(dsn)
        self._ensure_schema()

    @staticmethod
    def _parse_dsn(dsn: str) -> Dict[str, Any]:
        parsed = urlparse(dsn)
        if parsed.scheme not in {"mysql", "mariadb"}:
            raise ValueError("MariaDB DSN must use mysql:// or mariadb://")
        if not parsed.hostname or not parsed.path.strip("/"):
            raise ValueError("MariaDB DSN must include a host and database")
        return {
            "host": parsed.hostname,
            "port": parsed.port or 3306,
            "user": unquote(parsed.username or ""),
            "password": unquote(parsed.password or ""),
            "database": unquote(parsed.path.lstrip("/")),
            "charset": "utf8mb4",
            "cursorclass": DictCursor,
            "autocommit": False,
        }

    def _connect(self) -> Connection:
        return pymysql.connect(**self._connection_options)

    def _ensure_schema(self) -> None:
        statement = """
            CREATE TABLE IF NOT EXISTS blocks (
                `index` BIGINT NOT NULL PRIMARY KEY,
                parent_index BIGINT NULL,
                block_type VARCHAR(128) NOT NULL,
                data JSON NOT NULL,
                status VARCHAR(64) NOT NULL,
                children JSON NOT NULL,
                metadata JSON NOT NULL,
                hash CHAR(64) NOT NULL,
                previous_hash CHAR(64) NOT NULL,
                created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
                INDEX idx_blocks_status (status),
                INDEX idx_blocks_parent (parent_index),
                CONSTRAINT fk_blocks_parent
                    FOREIGN KEY (parent_index) REFERENCES blocks (`index`)
            ) ENGINE=InnoDB
        """
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(statement)
            connection.commit()

    @staticmethod
    def _values(block: Dict[str, Any]) -> tuple[Any, ...]:
        return (
            block["index"],
            block.get("parent_index"),
            block["block_type"],
            json.dumps(block.get("data", {}), sort_keys=True),
            block["status"],
            json.dumps(block.get("children", [])),
            json.dumps(block.get("metadata", {}), sort_keys=True),
            block["hash"],
            block["previous_hash"],
        )

    @staticmethod
    def _insert_sql() -> str:
        return """
            INSERT INTO blocks
                (`index`, parent_index, block_type, data, status, children,
                 metadata, hash, previous_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """

    def append_block(self, block: Dict[str, Any]) -> None:
        """Append exactly one block in its own transaction."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(self._insert_sql(), self._values(block))
            connection.commit()

    def load(self) -> List[Dict[str, Any]]:
        return self._load_where(None, ())

    def load_by_status(self, status: str) -> List[Dict[str, Any]]:
        """Return the indexed status projection, oldest block first."""
        return self._load_where("status = %s", (status,))

    def _load_where(self, clause: str | None, params: tuple[Any, ...]) -> List[Dict[str, Any]]:
        sql = """
            SELECT `index`, parent_index, block_type, data, status, children,
                   metadata, hash, previous_hash
            FROM blocks
        """
        if clause:
            sql += f" WHERE {clause}"
        sql += " ORDER BY `index`"
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                rows = cursor.fetchall()
        try:
            return [self._decode_row(row) for row in rows]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("Malformed ledger row in MariaDB") from exc

    @staticmethod
    def _decode_row(row: Dict[str, Any]) -> Dict[str, Any]:
        def decode(value: Any) -> Any:
            return json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value

        data = decode(row["data"])
        children = decode(row["children"])
        metadata = decode(row["metadata"])
        if not isinstance(data, dict) or not isinstance(metadata, dict) or not isinstance(children, list):
            raise ValueError("Ledger JSON columns have invalid shapes")
        return {
            "index": row["index"],
            "block_type": row["block_type"],
            "data": data,
            "status": row["status"],
            "previous_hash": row["previous_hash"],
            "parent_index": row["parent_index"],
            "children": children,
            "metadata": metadata,
            "hash": row["hash"],
        }

    def save(self, blocks: List[Dict[str, Any]]) -> None:
        """Replace the chain atomically for compatibility with LedgerStorage."""
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM blocks")
                if blocks:
                    cursor.executemany(self._insert_sql(), [self._values(block) for block in blocks])
            connection.commit()

    def clear(self) -> None:
        with self._connect() as connection:
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM blocks")
            connection.commit()
