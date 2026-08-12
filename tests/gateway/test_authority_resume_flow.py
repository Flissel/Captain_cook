"""Acceptance tests for the fail-closed authority resume flow."""
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from threading import Lock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

from blockchain.mariadb_storage import MariaDBStorage
from gateway.authority_resume_api import build_authority_resume_router
from gateway.authority_resume_contracts import AuthorityResumeError
from gateway.authority_resume_store import AuthorityResumeStore
from gateway.settings import GatewaySettings
from tests.support.mariadb import assert_isolated_test_database

TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="TEST_MARIADB_DSN is not configured"
)

NOW = datetime(2026, 8, 12, 9, 0, tzinfo=timezone.utc)
ASSEMBLY_A = "a" * 64
ASSEMBLY_B = "b" * 64
CAPTAIN_TOKEN = "captain-test-token"
WORKER_TOKEN = "worker-test-token"


@pytest.fixture
def storage() -> Iterator[MariaDBStorage]:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    value = MariaDBStorage(TEST_DSN)
    AuthorityResumeStore(value)
    with value.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM authority_dispatch_evidence")
            cursor.execute("DELETE FROM authority_resume_authorizations")
    value.clear()
    yield value
    with value.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute("DELETE FROM authority_dispatch_evidence")
            cursor.execute("DELETE FROM authority_resume_authorizations")
    value.clear()


def test_authorize_dispatch_readback_with_monotonic_revisions(
    storage: MariaDBStorage,
) -> None:
    store = AuthorityResumeStore(storage)
    first_auth, first_token = store.authorize(ASSEMBLY_A, now=NOW)
    second_auth, second_token = store.authorize(ASSEMBLY_A, now=NOW)
    assert first_auth.authorization_id != second_auth.authorization_id
    assert first_token != second_token
    first = store.dispatch(ASSEMBLY_A, first_token, now=NOW)
    second = store.dispatch(ASSEMBLY_A, second_token, now=NOW)
    assert (first.revision, second.revision) == (1, 2)
    evidence = store.readback(ASSEMBLY_A)
    assert evidence is not None
    assert evidence.revision == 2
    assert evidence.authorization_count == 2
    assert tuple(item.revision for item in evidence.dispatches) == (1, 2)


def test_authorization_is_single_use(storage: MariaDBStorage) -> None:
    store = AuthorityResumeStore(storage)
    _, token = store.authorize(ASSEMBLY_A, now=NOW)
    store.dispatch(ASSEMBLY_A, token, now=NOW)
    with pytest.raises(AuthorityResumeError) as excinfo:
        store.dispatch(ASSEMBLY_A, token, now=NOW)
    assert excinfo.value.reason == "already_consumed"


def test_expired_and_mismatched_authorizations_fail_closed(
    storage: MariaDBStorage,
) -> None:
    store = AuthorityResumeStore(storage)
    _, token = store.authorize(ASSEMBLY_A, now=NOW)
    with pytest.raises(AuthorityResumeError) as expired:
        store.dispatch(ASSEMBLY_A, token, now=NOW + timedelta(minutes=11))
    assert expired.value.reason == "expired"
    _, fresh_token = store.authorize(ASSEMBLY_A, now=NOW)
    with pytest.raises(AuthorityResumeError) as mismatch:
        store.dispatch(ASSEMBLY_B, fresh_token, now=NOW)
    assert mismatch.value.reason == "assembly_mismatch"
    with pytest.raises(AuthorityResumeError) as unknown:
        store.dispatch(ASSEMBLY_A, "never-issued", now=NOW)
    assert unknown.value.reason == "unknown_authorization"


def test_consumed_state_survives_process_restart(storage: MariaDBStorage) -> None:
    store = AuthorityResumeStore(storage)
    _, token = store.authorize(ASSEMBLY_A, now=NOW)
    store.dispatch(ASSEMBLY_A, token, now=NOW)
    assert TEST_DSN is not None
    restarted_storage = MariaDBStorage(TEST_DSN)
    restarted = AuthorityResumeStore(restarted_storage)
    with pytest.raises(AuthorityResumeError) as excinfo:
        restarted.dispatch(ASSEMBLY_A, token, now=NOW)
    assert excinfo.value.reason == "already_consumed"
    evidence = restarted.readback(ASSEMBLY_A)
    assert evidence is not None
    assert evidence.revision == 1


def test_readback_never_exposes_token_material(storage: MariaDBStorage) -> None:
    store = AuthorityResumeStore(storage)
    _, token = store.authorize(ASSEMBLY_A, now=NOW)
    store.dispatch(ASSEMBLY_A, token, now=NOW)
    evidence = store.readback(ASSEMBLY_A)
    assert evidence is not None
    serialized = evidence.model_dump_json()
    assert token not in serialized
    assert "token" not in serialized


@pytest.fixture
def client(storage: MariaDBStorage) -> TestClient:
    app = FastAPI()
    app.state.gateway_settings = GatewaySettings(
        ledger_dsn=SecretStr(TEST_DSN or ""),
        captain_gateway_token=SecretStr(CAPTAIN_TOKEN),
        worker_gateway_token=SecretStr(WORKER_TOKEN),
    )
    app.state.gateway_settings_lock = Lock()
    app.include_router(
        build_authority_resume_router(
            lambda: AuthorityResumeStore(storage), clock=lambda: NOW
        )
    )
    return TestClient(app)


def _captain(client: TestClient) -> dict[str, str]:
    return {"Authorization": f"Bearer {CAPTAIN_TOKEN}"}


def test_routes_require_captain_role(client: TestClient) -> None:
    unauthenticated = client.post(
        f"/v1/authority/assemblies/{ASSEMBLY_A}/resume-authorizations"
    )
    assert unauthenticated.status_code == 401
    worker = client.post(
        f"/v1/authority/assemblies/{ASSEMBLY_A}/resume-authorizations",
        headers={"Authorization": f"Bearer {WORKER_TOKEN}"},
    )
    assert worker.status_code == 403


def test_dispatch_without_authorization_is_denied(client: TestClient) -> None:
    denied = client.post(
        f"/v1/authority/assemblies/{ASSEMBLY_A}/dispatches",
        headers=_captain(client),
        json={"token": "never-issued"},
    )
    assert denied.status_code == 403
    assert denied.json()["detail"] == "unknown_authorization"


def test_double_dispatch_conflicts_and_readback_reports_evidence(
    client: TestClient,
) -> None:
    issued = client.post(
        f"/v1/authority/assemblies/{ASSEMBLY_A}/resume-authorizations",
        headers=_captain(client),
    )
    assert issued.status_code == 201
    token = issued.json()["token"]
    first = client.post(
        f"/v1/authority/assemblies/{ASSEMBLY_A}/dispatches",
        headers=_captain(client),
        json={"token": token},
    )
    assert first.status_code == 201
    assert first.json()["revision"] == 1
    second = client.post(
        f"/v1/authority/assemblies/{ASSEMBLY_A}/dispatches",
        headers=_captain(client),
        json={"token": token},
    )
    assert second.status_code == 409
    evidence = client.get(
        f"/v1/authority/assemblies/{ASSEMBLY_A}/readback",
        headers=_captain(client),
    )
    assert evidence.status_code == 200
    payload = evidence.json()
    assert payload["revision"] == 1
    assert payload["authorization_count"] == 1
    assert token not in evidence.text


def test_readback_of_unknown_assembly_is_not_found(client: TestClient) -> None:
    missing = client.get(
        f"/v1/authority/assemblies/{ASSEMBLY_B}/readback",
        headers=_captain(client),
    )
    assert missing.status_code == 404
    malformed = client.get(
        "/v1/authority/assemblies/not-a-digest/readback",
        headers=_captain(client),
    )
    assert malformed.status_code == 404
