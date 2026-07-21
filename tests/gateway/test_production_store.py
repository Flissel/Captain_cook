from __future__ import annotations

from gateway.production_store import LazyGatewayStore


def test_lazy_gateway_store_does_not_connect_during_composition() -> None:
    store = LazyGatewayStore("mariadb://u:p@127.0.0.1:9/captain_test")

    assert store.configured_database == "captain_test"
