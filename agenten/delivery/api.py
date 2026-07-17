"""Production delivery composition boundary.

The former SQLite FastAPI control plane is intentionally unavailable here.
Production orchestration consumes :class:`GatewayDeliveryClient`; historical
SQLite inspection lives behind the explicitly named ``legacy_api`` module.
"""

from __future__ import annotations

from typing import NoReturn


def create_delivery_app(*args: object, **kwargs: object) -> NoReturn:
    del args, kwargs
    raise RuntimeError(
        "SQLite delivery ledger is legacy-import only; use GatewayDeliveryClient"
    )
