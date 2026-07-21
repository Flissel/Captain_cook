"""Gateway-owned constructors for the isolated local Captain test authority."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from urllib.parse import unquote, urlsplit

from blockchain.mariadb_storage import MariaDBStorage
from gateway.store import GatewayStore


class LazyGatewayStore:
    """Delay MariaDB connection/schema work until the first Gateway operation."""

    def __init__(self, dsn: str) -> None:
        parsed = urlsplit(dsn)
        database = unquote(parsed.path.lstrip("/"))
        if (
            parsed.scheme not in {"mysql", "mariadb"}
            or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
            or database != "captain_test"
        ):
            raise ValueError("production Factory store requires loopback captain_test")
        self._dsn = dsn
        self._database = database
        self._store: GatewayStore | None = None

    @property
    def configured_database(self) -> str:
        return self._database

    def _resolved(self) -> GatewayStore:
        if self._store is None:
            self._store = GatewayStore(MariaDBStorage(self._dsn))
        return self._store

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolved(), name)


def build_local_captain_test_gateway_store(
    dsn: str,
    *,
    clock: Callable[[], datetime],
) -> GatewayStore:
    """Build Gateway only for the isolated local ``captain_test`` database."""

    parsed = urlsplit(dsn)
    if (
        parsed.scheme not in {"mysql", "mariadb"}
        or (parsed.hostname or "").lower()
        not in {"127.0.0.1", "localhost", "::1"}
        or unquote(parsed.path.lstrip("/")) != "captain_test"
    ):
        raise ValueError("live capability evidence requires local captain_test")
    return GatewayStore(MariaDBStorage(dsn), clock=clock)
