"""Core gateway app over MariaDB without the production service stack.

Used by local spikes (plan 2026-11-07, Phase 5) that need only the ledger
routes. The production builder also wires provider, billing and
capability-assembly services and refuses to start without their config.
"""

from __future__ import annotations

from fastapi import FastAPI

from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import create_app, verify_ledger_before_serving
from gateway.settings import GatewaySettings


class NullMirror:
    """No Minibook in the spike: drop projections instead of queueing them."""

    def enqueue_nowait(self, block: object) -> None:
        del block


def create_core_app_from_environment() -> tuple[FastAPI, GatewaySettings]:
    settings = GatewaySettings.from_env()
    storage = MariaDBStorage(settings.ledger_dsn.get_secret_value())
    verify_ledger_before_serving(storage)
    return create_app(storage=storage, mirror=NullMirror(), settings=settings), settings
