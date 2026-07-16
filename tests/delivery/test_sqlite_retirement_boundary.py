from pathlib import Path

import pytest

from agenten.delivery.api import create_delivery_app
from agenten.delivery.ledger import SqliteDeliveryLedger
from agenten.delivery.legacy_api import create_legacy_delivery_app


def test_production_delivery_api_rejects_sqlite(tmp_path: Path) -> None:
    ledger = SqliteDeliveryLedger(tmp_path / "legacy.db")

    with pytest.raises(RuntimeError, match="legacy-import only"):
        create_delivery_app(ledger)


def test_legacy_api_requires_explicit_legacy_import(tmp_path: Path) -> None:
    ledger = SqliteDeliveryLedger(tmp_path / "legacy.db")

    assert create_legacy_delivery_app(ledger).title.startswith("Legacy")


def test_production_delivery_modules_do_not_import_sqlite_ledger() -> None:
    root = Path(__file__).parents[2] / "agenten" / "delivery"
    for name in ("__init__.py", "api.py", "gateway_client.py", "service.py"):
        assert "SqliteDeliveryLedger" not in (root / name).read_text(encoding="utf-8")
