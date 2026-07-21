"""Explicit, destructive captain_test proof for the capability factory authority chain."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

pytestmark = [pytest.mark.live, pytest.mark.db_mutating]


class SimulatedProcessCrash(RuntimeError):
    pass


def _explicit_isolated_test_dsn() -> str:
    """Accept only the process environment; never discover or load a .env file."""

    value = os.environ.get("TEST_MARIADB_DSN", "").strip()
    if not value:
        pytest.fail(
            "destructive live proof requires explicitly exported TEST_MARIADB_DSN"
        )
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"mysql", "mariadb"}
        or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
        or parsed.path.strip("/") != "captain_test"
    ):
        pytest.fail(
            "TEST_MARIADB_DSN must target loopback database exactly captain_test"
        )
    return value


def test_capability_authority_transactions_survive_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = _explicit_isolated_test_dsn()
    from blockchain.mariadb_storage import MariaDBStorage
    from gateway.store import GatewayStore
    from tests.gateway import test_agent_factory as gateway_acceptance

    monkeypatch.setattr(gateway_acceptance, "TEST_DSN", dsn)
    storage = MariaDBStorage(dsn)
    storage.clear()
    try:
        release = gateway_acceptance._prepare_v2_capability_release(
            storage,
            promote=True,
        )
        store = GatewayStore(
            storage,
            clock=lambda: gateway_acceptance.TEST_GATEWAY_NOW,
        )
        insert = store._insert

        def crash_inside_publication(cursor: object, block: dict[str, object]) -> None:
            insert(cursor, block)
            if block["block_type"] == "capability_package_published":
                raise SimulatedProcessCrash("inside atomic publication")

        monkeypatch.setattr(store, "_insert", crash_inside_publication)
        with pytest.raises(SimulatedProcessCrash, match="atomic publication"):
            store.publish_capability_release(release)
        assert store.factory_terminal_decision(release.package.factory_job_id) is None
        assert store.capability(release.package.capability_id) is None

        monkeypatch.setattr(store, "_insert", insert)
        published = store.publish_capability_release(release)
        restarted = GatewayStore(
            storage,
            clock=lambda: gateway_acceptance.TEST_GATEWAY_NOW,
        )
        replayed = restarted.publish_capability_release(release)
        authority = restarted.capability(
            release.package.capability_id,
            version=release.package.capability_version,
        )
        assert published.replayed is False
        assert replayed.replayed is True
        assert authority is not None
        assert authority.release_authority_job_id == release.package.factory_job_id
        assert authority.terminal_decision_id == release.decision.decision_id
        assert authority.package_ref == release.package_ref

        command, result = gateway_acceptance._seed_runtime_result(storage, release)
        execution = gateway_acceptance._execution_request(release, command, result)
        recorded = restarted.record_capability_execution(execution)
        execution_replay = GatewayStore(storage).record_capability_execution(execution)
        readback = restarted.capability_execution(execution.outcome.command_id)
        assert recorded.replayed is False
        assert execution_replay.replayed is True
        assert readback is not None
        assert readback.outcome == execution.outcome
    finally:
        storage.clear()
