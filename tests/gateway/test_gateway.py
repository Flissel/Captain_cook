import hashlib
import inspect
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Event
from typing import Any
from uuid import UUID

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import SecretStr
from pymysql.cursors import DictCursor
from pymysql.err import OperationalError

from blockchain.Blockchain_modell import Block
from blockchain.mariadb_storage import MariaDBStorage
from gateway.app import CAPTAIN_WRITE_BLOCK_TYPES, BlockRequest, create_app
from gateway.auth import GatewayRole, require_actor
from gateway.contracts import DeliveryEventEnvelope, ReleaseProjection
from gateway.settings import GatewaySettings
from gateway.store import GatewayStore
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
if os.getenv("REQUIRE_MARIADB_TESTS") == "1":
    assert_isolated_test_database(TEST_DSN)
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")

TERMINAL_OUTCOMES = (
    "succeeded",
    "failed",
    "rejected",
    "cancelled",
    "failed_after_max_iterations",
    "aborted_infra",
)


class RecordingMirror:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.items: list[dict[str, Any]] = []

    def enqueue_nowait(self, block: dict[str, Any]) -> None:
        if self.fail:
            raise RuntimeError("minibook unavailable")
        self.items.append(block)


@pytest.fixture
def storage() -> MariaDBStorage:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    result = MariaDBStorage(TEST_DSN)
    assert_isolated_test_database(TEST_DSN)
    result.clear()
    yield result
    assert_isolated_test_database(TEST_DSN)
    result.clear()


@pytest.fixture
def client(storage: MariaDBStorage) -> TestClient:
    return TestClient(legacy_application(storage=storage, mirror=RecordingMirror()))


async def legacy_test_actor(request: Request) -> GatewayRole:
    if request.url.path == "/blocks":
        payload = await request.json()
        if isinstance(payload, dict) and payload.get("block_type") in CAPTAIN_WRITE_BLOCK_TYPES:
            return GatewayRole.CAPTAIN
        return GatewayRole.WORKER
    if "/claim" in request.url.path or request.url.path == "/sink/crm":
        return GatewayRole.WORKER
    return GatewayRole.CAPTAIN


def legacy_application(
    *,
    storage: MariaDBStorage,
    mirror: RecordingMirror,
    approval_enabled: bool = True,
) -> FastAPI:
    assert TEST_DSN is not None
    settings = GatewaySettings(
        ledger_dsn=SecretStr(TEST_DSN),
        captain_gateway_token=SecretStr("legacy-captain-token"),
        worker_gateway_token=SecretStr("legacy-worker-token"),
        approval_enabled=approval_enabled,
    )
    application = create_app(storage=storage, mirror=mirror, settings=settings)
    application.dependency_overrides[require_actor] = legacy_test_actor
    return application


def batch_payload(batch_id: str = "batch-1", title: str = "Build the notification workflow") -> dict[str, Any]:
    return {
        "batch_id": batch_id,
        "title": title,
        "goal": "Deliver a tested workflow",
        "subtask_ids": [f"subtask-{batch_id}"],
        "target": "n8n",
        "capability_tags": ["notifications", "email"],
        "depends_on": [],
        "constraints": [],
        "acceptance_criteria": [
            {"assertion_id": "status-ok", "kind": "status_equals", "expected": "succeeded"}
        ],
        "golden_cases": [],
    }


def create_batch(client: TestClient, batch_id: str = "batch-1", status: str = "pending") -> int:
    response = client.post(
        "/blocks",
        json={"block_type": "work_batch", "status": status, "data": batch_payload(batch_id)},
    )
    assert response.status_code == 201, response.text
    return response.json()["index"]


def claim(client: TestClient, batch_id: str = "batch-1") -> str:
    response = client.post(f"/batches/{batch_id}/claim")
    assert response.status_code == 200, response.text
    return response.json()["claim_token"]


def worker_block(
    client: TestClient,
    token: str | None,
    *,
    block_type: str = "codex_session",
    status: str = "in_progress",
    data: dict[str, Any] | None = None,
) -> Any:
    headers = {"X-Claim-Token": token} if token else {}
    return client.post(
        "/blocks",
        headers=headers,
        json={
            "block_type": block_type,
            "status": status,
            "data": data or {"batch_id": "batch-1", "iteration": 1},
        },
    )


def review_batch(
    client: TestClient,
    *,
    iteration: int = 1,
    decision: str = "passed",
    review_id: str | None = None,
) -> Any:
    identifier = review_id or f"review-{iteration}-{decision}"
    return client.post(
        "/batches/batch-1/review",
        json={
            "batch_id": "batch-1",
            "iteration": iteration,
            "review_id": identifier,
            "decision": decision,
            "evidence_refs": [f"artifact://validation/{iteration}"],
        },
    )


def test_legacy_import_route_is_idempotent_and_conflict_safe(client: TestClient) -> None:
    todo = {
        "legacy_record_id": "todo:todo-1",
        "batch_id": "legacy-todo1",
        "record_type": "todo",
        "data": {
            "batch_id": "legacy-todo1",
            "legacy_todo_id": "todo-1",
            "todo": {"title": "Historical delivery"},
        },
    }
    event = {
        "legacy_record_id": "event:event-1",
        "batch_id": "legacy-todo1",
        "record_type": "event",
        "data": {
            "batch_id": "legacy-todo1",
            "legacy_todo_id": "todo-1",
            "legacy_event_id": "event-1",
            "actor": "captain",
            "event_type": "todo_created",
            "payload": {"version": 1},
            "created_at": "2026-07-16T12:00:00Z",
            "legacy_sequence": 1,
        },
    }

    first_todo = client.post("/imports/legacy-delivery", json=todo)
    first_event = client.post("/imports/legacy-delivery", json=event)
    replay_event = client.post("/imports/legacy-delivery", json=event)

    assert first_todo.status_code == 201, first_todo.text
    assert first_todo.json()["created"] is True
    assert first_event.status_code == 201, first_event.text
    assert first_event.json()["created"] is True
    assert replay_event.status_code == 201, replay_event.text
    assert replay_event.json()["created"] is False
    assert first_event.json()["block"] == replay_event.json()["block"]

    conflicting = event | {"data": event["data"] | {"actor": "other"}}
    response = client.post("/imports/legacy-delivery", json=conflicting)
    assert response.status_code == 409


def test_claim_and_completion_append_events_without_mutating_work_batch(
    client: TestClient,
    storage: MariaDBStorage,
) -> None:
    parent_index = create_batch(client)
    parent_before = storage.load()[0]
    token = claim(client)
    validation = worker_block(
        client,
        token,
        block_type="validation_run",
        data={
            "batch_id": "batch-1",
            "iteration": 1,
            "report_ref": "validation-1",
            "artifact_ref": "artifact://validation/1",
        },
    )
    review = review_batch(client)
    done = worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={
            "batch_id": "batch-1",
            "outcome": "succeeded",
            "artifact_ref": "workflow-42",
            "capabilities": ["email"],
            "eval_score": 9,
        },
    )

    assert validation.status_code == 201, validation.text
    assert review.status_code == 201, review.text
    assert done.status_code == 201, done.text
    blocks = storage.load()
    parent_after = next(block for block in blocks if block["index"] == parent_index)
    lifecycle = [block for block in blocks if block["parent_index"] == parent_index]
    assert parent_after == parent_before
    assert parent_after["status"] == "pending"
    assert parent_after["metadata"] == {}
    assert parent_after["children"] == []
    assert [block["block_type"] for block in lifecycle] == [
        "batch_claimed",
        "validation_run",
        "review_decision",
        "batch_done",
    ]
    claim_data = lifecycle[0]["data"]
    assert set(claim_data) == {"batch_id", "claim_token_sha256", "claim_expires_at"}
    assert claim_data["claim_token_sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
    assert token not in json.dumps(blocks, default=str)

    detail = client.get("/batches/batch-1")
    assert detail.status_code == 200, detail.text
    assert detail.json()["status"] == "succeeded"
    assert detail.json()["claim_iteration"] == 1


@pytest.mark.parametrize("source_path", [Path("gateway/app.py"), Path("gateway/store.py")])
def test_gateway_sources_do_not_issue_lifecycle_update_sql(source_path: Path) -> None:
    assert source_path.exists(), f"missing extracted gateway source: {source_path}"
    normalized = re.sub(r"\s+", " ", source_path.read_text(encoding="utf-8").lower())

    assert re.search(r"update blocks set (?:status|metadata|children)\b", normalized) is None


def test_gateway_store_is_extracted_from_the_fastapi_module() -> None:
    app_source = Path("gateway/app.py").read_text(encoding="utf-8")

    assert "class GatewayStore" not in app_source
    assert app_source.count("cursor.execute") == 1
    assert 'cursor.execute("SELECT 1")' in app_source
    assert Path("gateway/store.py").exists()


def test_gateway_executes_no_lifecycle_update_sql(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []
    original_execute = DictCursor.execute

    def recording_execute(self: DictCursor, query: str, args: Any = None) -> int:
        statements.append(query)
        return original_execute(self, query, args)

    monkeypatch.setattr(DictCursor, "execute", recording_execute)
    create_batch(client)
    token = claim(client)
    assert client.post(
        "/batches/batch-1/claim/heartbeat",
        headers={"X-Claim-Token": token},
    ).status_code == 200
    assert worker_block(
        client,
        token,
        block_type="validation_run",
        data={"batch_id": "batch-1", "iteration": 1, "artifact_ref": "artifact://validation/1"},
    ).status_code == 201
    assert review_batch(client).status_code == 201
    assert worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "succeeded"},
    ).status_code == 201

    forbidden = [
        statement
        for statement in statements
        if re.search(
            r"update\s+blocks\s+set\s+(?:status|metadata|children)\b",
            re.sub(r"\s+", " ", statement.lower()),
        )
    ]
    assert forbidden == []


def test_two_workers_cannot_claim_the_same_batch(storage: MariaDBStorage) -> None:
    client = TestClient(legacy_application(storage=storage, mirror=RecordingMirror()))
    create_batch(client)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: client.post("/batches/batch-1/claim"), range(2)))

    assert sorted(response.status_code for response in responses) == [200, 409]


def test_duplicate_batch_ids_are_rejected(client: TestClient) -> None:
    create_batch(client)

    response = client.post(
        "/blocks",
        json={"block_type": "work_batch", "data": batch_payload(title="Duplicate")},
    )

    assert response.status_code == 409


def test_identical_canonical_work_batch_replay_returns_existing_block(
    client: TestClient,
    storage: MariaDBStorage,
) -> None:
    first = client.post(
        "/blocks",
        json={"block_type": "work_batch", "data": batch_payload()},
    )
    assert first.status_code == 201
    existing = first.json()
    before_replay = storage.load()

    replay = client.post(
        "/blocks",
        json={
            "block_type": "work_batch",
            "status": existing["status"],
            "data": existing["data"],
            "metadata": existing["metadata"],
        },
    )

    assert replay.status_code == 201, replay.text
    assert replay.json() == existing
    assert storage.load() == before_replay

    next_index = create_batch(client, "batch-2")
    next_block = next(block for block in storage.load() if block["index"] == next_index)
    assert next_block["index"] == existing["index"] + 1
    assert next_block["previous_hash"] == existing["hash"]


def test_concurrent_duplicate_batch_creation_persists_exactly_one_root(
    storage: MariaDBStorage,
) -> None:
    client = TestClient(legacy_application(storage=storage, mirror=RecordingMirror()))

    def create(_: int) -> Any:
        return client.post(
            "/blocks",
            json={"block_type": "work_batch", "data": batch_payload()},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(create, range(2)))

    assert [response.status_code for response in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    roots = [block for block in storage.load() if block["block_type"] == "work_batch"]
    assert len(roots) == 1


def test_parallel_gateway_writes_preserve_immediate_hash_adjacency(
    storage: MariaDBStorage,
) -> None:
    client = TestClient(legacy_application(storage=storage, mirror=RecordingMirror()))
    create_batch(client, "batch-1")
    create_batch(client, "batch-2")
    tokens = {batch_id: claim(client, batch_id) for batch_id in ("batch-1", "batch-2")}

    def append_session(batch_id: str) -> Any:
        return worker_block(
            client,
            tokens[batch_id],
            data={"batch_id": batch_id, "iteration": 1},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(append_session, ("batch-1", "batch-2")))

    assert [response.status_code for response in responses] == [201, 201]
    blocks = storage.load()
    assert blocks[0]["previous_hash"] == "0"
    assert all(
        current["previous_hash"] == previous["hash"]
        for previous, current in zip(blocks, blocks[1:])
    )


def test_concurrent_claim_and_first_holdout_share_global_lock_order(
    storage: MariaDBStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = TestClient(legacy_application(storage=storage, mirror=RecordingMirror()))
    parent_index = create_batch(client)
    parent_before = storage.load()[0]
    claim_first_lock = Event()
    holdout_lock_attempted = Event()
    holdout_lock_acquired = Event()
    release_claim = Event()
    first_claim_lock: list[str] = []
    deadlock_victims: list[str] = []
    original_execute = DictCursor.execute

    def lock_target(query: str) -> str | None:
        normalized = re.sub(r"\s+", " ", query.lower()).strip()
        if "select next_block_index from ledger_state where id = 1 for update" in normalized:
            return "global-index"
        if (
            "from blocks" in normalized
            and "block_type = 'work_batch'" in normalized
            and normalized.endswith("for update")
        ):
            return "batch-parent"
        return None

    def coordinating_execute(self: DictCursor, query: str, args: Any = None) -> int:
        stack_functions = {frame.function for frame in inspect.stack()}
        operation = (
            "claim"
            if "_claim_once" in stack_functions
            else "holdout"
            if "_append_once" in stack_functions
            else ""
        )
        target = lock_target(query)
        first_holdout_lock = (
            operation == "holdout"
            and target is not None
            and not holdout_lock_attempted.is_set()
        )
        if first_holdout_lock:
            holdout_lock_attempted.set()
        try:
            result = original_execute(self, query, args)
        except OperationalError as exc:
            if exc.args and exc.args[0] == 1213:
                deadlock_victims.append(operation)
            raise
        if operation == "claim" and target is not None and not claim_first_lock.is_set():
            first_claim_lock.append(target)
            claim_first_lock.set()
            assert release_claim.wait(timeout=5), "claim transaction was not released"
        if first_holdout_lock:
            holdout_lock_acquired.set()
        return result

    monkeypatch.setattr(DictCursor, "execute", coordinating_execute)

    def issue_claim() -> Any:
        return client.post("/batches/batch-1/claim")

    def insert_holdout() -> Any:
        return client.post(
            "/blocks",
            json={
                "block_type": "holdout",
                "parent_index": parent_index,
                "data": {
                    "batch_id": "batch-1",
                    "cases": [{"case_id": "secret-1", "input": {"lead": 1}}],
                },
            },
        )

    pool = ThreadPoolExecutor(max_workers=2)
    try:
        claim_future = pool.submit(issue_claim)
        assert claim_first_lock.wait(timeout=5), "claim did not acquire its first database lock"
        holdout_future = pool.submit(insert_holdout)
        assert holdout_lock_attempted.wait(timeout=5), "holdout did not attempt its first database lock"
        if first_claim_lock == ["batch-parent"]:
            assert holdout_lock_acquired.wait(timeout=5), "holdout did not acquire the global index lock"
        release_claim.set()
        claim_response = claim_future.result(timeout=5)
        holdout_response = holdout_future.result(timeout=5)
    finally:
        release_claim.set()
        pool.shutdown(wait=False, cancel_futures=True)

    assert deadlock_victims == []
    assert first_claim_lock == ["global-index"]
    assert claim_response.status_code == 200, claim_response.text
    assert holdout_response.status_code == 201, holdout_response.text

    blocks = storage.load()
    assert next(block for block in blocks if block["index"] == parent_index) == parent_before
    claim_event = next(block for block in blocks if block["block_type"] == "batch_claimed")
    first_holdout = next(block for block in blocks if block["block_type"] == "holdout")
    assert first_holdout["index"] == claim_event["index"] + 1
    assert first_holdout["previous_hash"] == claim_event["hash"]
    assert all(block["hash"] == Block(**block).compute_hash() for block in blocks)
    assert all(
        current["previous_hash"] == previous["hash"]
        for previous, current in zip(blocks, blocks[1:])
    )

    replacement = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent_index,
            "data": {
                "batch_id": "batch-1",
                "cases": [{"case_id": "replacement", "input": {}}],
            },
        },
    )
    assert replacement.status_code == 409
    assert next(block for block in storage.load() if block["block_type"] == "holdout") == first_holdout


def test_append_retries_only_documented_transaction_errors(
    storage: MariaDBStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GatewayStore(storage)
    request = BlockRequest(block_type="work_batch", data=batch_payload())
    original = store._append_once
    attempts = 0

    def transient_then_success(request: BlockRequest, claim_token: str | None) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OperationalError(1213, "deadlock victim")
        return original(request, claim_token)

    monkeypatch.setattr(store, "_append_once", transient_then_success)
    assert store.append(request, None)["block_type"] == "work_batch"
    assert attempts == 3

    monkeypatch.setattr(
        store,
        "_append_once",
        lambda request, claim_token: (_ for _ in ()).throw(OperationalError(9999, "other")),
    )
    with pytest.raises(OperationalError) as error:
        store.append(BlockRequest(block_type="work_batch", data=batch_payload("batch-2")), None)
    assert error.value.args[0] == 9999

    exhausted_attempts = 0

    def always_transient(request: BlockRequest, claim_token: str | None) -> dict[str, Any]:
        nonlocal exhausted_attempts
        exhausted_attempts += 1
        raise OperationalError(1213, "persistent deadlock")

    monkeypatch.setattr(store, "_append_once", always_transient)
    with pytest.raises(OperationalError) as exhausted:
        store.append(BlockRequest(block_type="work_batch", data=batch_payload("batch-3")), None)
    assert exhausted.value.args[0] == 1213
    assert exhausted_attempts == 3


def test_claim_heartbeat_and_approval_share_bounded_transaction_retry(
    storage: MariaDBStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = GatewayStore(storage)

    def probe(result: Any) -> tuple[list[int], Any]:
        attempts: list[int] = []

        def operation(*_: Any) -> Any:
            attempts.append(len(attempts) + 1)
            if len(attempts) == 1:
                raise OperationalError(1020, "stale transaction read")
            return result

        return attempts, operation

    claim_attempts, claim_once = probe({"claim_token": "token", "claim_expires_at": "later"})
    monkeypatch.setattr(store, "_claim_once", claim_once)
    assert store.claim("batch-1")["claim_token"] == "token"
    assert claim_attempts == [1, 2]

    heartbeat_attempts, heartbeat_once = probe({"claim_expires_at": "later"})
    monkeypatch.setattr(store, "_heartbeat_once", heartbeat_once)
    assert store.heartbeat("batch-1", "token") == {"claim_expires_at": "later"}
    assert heartbeat_attempts == [1, 2]

    approval_attempts, approval_once = probe(None)
    monkeypatch.setattr(store, "_approve_once", approval_once)
    assert store.approve("batch-1") is None
    assert approval_attempts == [1, 2]


@pytest.mark.parametrize(
    ("block_type", "data"),
    [
        (
            "batch_claimed",
            {
                "batch_id": "batch-1",
                "claim_token_sha256": "0" * 64,
                "claim_expires_at": "2026-07-17T00:00:00Z",
            },
        ),
        (
            "batch_heartbeat",
            {"batch_id": "batch-1", "claim_expires_at": "2026-07-17T00:00:00Z"},
        ),
        ("batch_approved", {"batch_id": "batch-1"}),
    ],
)
def test_generic_blocks_reject_gateway_owned_lifecycle_events(
    client: TestClient,
    storage: MariaDBStorage,
    block_type: str,
    data: dict[str, Any],
) -> None:
    response = client.post(
        "/blocks",
        json={
            "block_type": block_type,
            "status": "recorded",
            "data": data,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == f"{block_type} must use its dedicated gateway route"
    assert storage.load() == []


def test_invalid_initial_work_batch_status_is_rejected_before_insert(
    client: TestClient,
    storage: MariaDBStorage,
) -> None:
    response = client.post(
        "/blocks",
        json={
            "block_type": "work_batch",
            "status": "succeeded",
            "data": batch_payload(),
        },
    )

    assert response.status_code == 422
    assert storage.load() == []


def test_second_holdout_cannot_replace_the_immutable_suite(client: TestClient) -> None:
    parent = create_batch(client)
    first = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent,
            "data": {
                "batch_id": "batch-1",
                "cases": [{"case_id": "secret-1", "input": {"version": 1}}],
            },
        },
    )
    second = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent,
            "data": {
                "batch_id": "batch-1",
                "cases": [{"case_id": "secret-2", "input": {"version": 2}}],
            },
        },
    )

    assert first.status_code == 201
    assert second.status_code == 409
    token = claim(client)
    assert worker_block(client, token).status_code == 201
    released = client.get("/batches/batch-1/holdout", headers={"X-Claim-Token": token})
    assert released.status_code == 410
    assert "secret-1" not in released.text


def test_identical_canonical_holdout_replay_returns_existing_block(
    client: TestClient,
    storage: MariaDBStorage,
) -> None:
    parent = create_batch(client)
    first = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent,
            "data": {
                "batch_id": "batch-1",
                "cases": [{"case_id": "secret-1", "input": {"version": 1}}],
            },
        },
    )
    assert first.status_code == 201
    existing = first.json()
    before_replay = storage.load()

    replay = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "status": existing["status"],
            "parent_index": parent,
            "data": existing["data"],
            "metadata": existing["metadata"],
        },
    )

    assert replay.status_code == 201, replay.text
    assert replay.json() == existing
    assert storage.load() == before_replay


def test_identical_holdout_replay_with_different_parent_remains_conflict(
    client: TestClient,
    storage: MariaDBStorage,
) -> None:
    parent = create_batch(client, "batch-1")
    other_parent = create_batch(client, "batch-2")
    first = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent,
            "data": {
                "batch_id": "batch-1",
                "cases": [{"case_id": "secret-1", "input": {"version": 1}}],
            },
        },
    )
    assert first.status_code == 201
    before_replay = storage.load()

    replay = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": other_parent,
            "data": first.json()["data"],
        },
    )

    assert replay.status_code == 409
    assert storage.load() == before_replay


def test_work_batch_must_satisfy_the_captain_contract(client: TestClient) -> None:
    response = client.post(
        "/blocks",
        json={"block_type": "work_batch", "data": {"batch_id": "batch-1", "title": "Incomplete"}},
    )

    assert response.status_code == 422


def test_holdout_must_satisfy_the_captain_contract(client: TestClient) -> None:
    parent = create_batch(client)

    response = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "missing-input"}]},
        },
    )

    assert response.status_code == 422


def test_worker_blocks_require_the_current_fencing_token(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)

    assert worker_block(client, None).status_code == 409
    assert worker_block(client, "stale-token").status_code == 409
    assert worker_block(client, token).status_code == 201


def test_fenced_block_cannot_attach_to_another_batch(client: TestClient) -> None:
    create_batch(client, "batch-1")
    other_parent = create_batch(client, "batch-2")
    token = claim(client, "batch-1")

    response = client.post(
        "/blocks",
        headers={"X-Claim-Token": token},
        json={
            "block_type": "codex_session",
            "parent_index": other_parent,
            "data": {"batch_id": "batch-1", "iteration": 1},
        },
    )

    assert response.status_code == 409


def test_claim_token_is_not_persisted_or_exposed(client: TestClient, storage: MariaDBStorage) -> None:
    create_batch(client)
    token = claim(client)

    persisted = storage.load()
    claim_event = next(block for block in persisted if block["block_type"] == "batch_claimed")
    response = client.get("/batches/batch-1/blocks")

    assert token not in str(persisted)
    assert token not in response.text
    assert '"claim_token":' not in response.text
    assert set(claim_event["data"]) == {"batch_id", "claim_token_sha256", "claim_expires_at"}
    assert claim_event["data"]["claim_token_sha256"] == hashlib.sha256(token.encode("utf-8")).hexdigest()


def test_legacy_holdout_route_never_discloses_after_a_codex_session(
    client: TestClient,
) -> None:
    parent_index = create_batch(client)
    holdout = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent_index,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "secret-1", "input": {"lead": 1}}]},
        },
    )
    assert holdout.status_code == 201
    token = claim(client)
    headers = {"X-Claim-Token": token}

    assert client.get("/batches/batch-1/holdout", headers=headers).status_code == 410
    assert worker_block(client, token).status_code == 201

    response = client.get("/batches/batch-1/holdout", headers=headers)
    assert response.status_code == 410
    assert "secret-1" not in response.text


def test_legacy_holdout_route_remains_gone_across_claim_iterations(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import store as store_module

    start = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store_module, "_utcnow", lambda: start)
    parent_index = create_batch(client)
    assert client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent_index,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "secret-1", "input": {}}]},
        },
    ).status_code == 201
    first_token = claim(client)
    assert worker_block(client, first_token).status_code == 201
    assert client.get(
        "/batches/batch-1/holdout",
        headers={"X-Claim-Token": first_token},
    ).status_code == 410

    monkeypatch.setattr(store_module, "_utcnow", lambda: start + timedelta(minutes=91))
    second_token = claim(client)
    second_headers = {"X-Claim-Token": second_token}
    assert client.get("/batches/batch-1/holdout", headers=second_headers).status_code == 410
    assert worker_block(
        client,
        second_token,
        data={"batch_id": "batch-1", "iteration": 2},
    ).status_code == 201
    assert client.get("/batches/batch-1/holdout", headers=second_headers).status_code == 410


def test_work_batch_and_holdout_remain_immutable_across_lifecycle(
    client: TestClient,
    storage: MariaDBStorage,
) -> None:
    parent_index = create_batch(client)
    holdout_response = client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent_index,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "secret-1", "input": {}}]},
        },
    )
    assert holdout_response.status_code == 201
    holdout_index = holdout_response.json()["index"]
    before = {block["index"]: block for block in storage.load()}

    token = claim(client)
    assert client.post(
        "/batches/batch-1/claim/heartbeat",
        headers={"X-Claim-Token": token},
    ).status_code == 200
    assert worker_block(client, token).status_code == 201
    assert worker_block(
        client,
        token,
        block_type="validation_run",
        data={"batch_id": "batch-1", "iteration": 1, "artifact_ref": "artifact://validation/1"},
    ).status_code == 201
    assert review_batch(client).status_code == 201
    assert worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "succeeded"},
    ).status_code == 201

    after = {block["index"]: block for block in storage.load()}
    assert after[parent_index] == before[parent_index]
    assert after[holdout_index] == before[holdout_index]


def test_bundle_and_blocks_never_leak_holdout(client: TestClient) -> None:
    parent_index = create_batch(client)
    client.post(
        "/blocks",
        json={
            "block_type": "holdout",
            "parent_index": parent_index,
            "data": {"batch_id": "batch-1", "cases": [{"case_id": "secret-1", "input": {"lead": 1}}]},
        },
    )

    bundle = client.get("/batches/batch-1/bundle")
    blocks = client.get("/batches/batch-1/blocks")

    assert bundle.status_code == 200
    assert "holdout" not in bundle.json()
    assert blocks.status_code == 200
    assert all(block["block_type"] != "holdout" for block in blocks.json())
    assert "secret-1" not in blocks.text


@pytest.mark.parametrize("outcome", TERMINAL_OUTCOMES)
def test_all_terminal_outcomes_are_projected_and_fenced(client: TestClient, outcome: str) -> None:
    create_batch(client)
    token = claim(client)
    if outcome == "succeeded":
        assert worker_block(
            client,
            token,
            block_type="validation_run",
            data={"batch_id": "batch-1", "iteration": 1, "artifact_ref": "artifact://validation/1"},
        ).status_code == 201
        assert review_batch(client).status_code == 201
    elif outcome == "failed_after_max_iterations":
        assert worker_block(
            client,
            token,
            block_type="validation_run",
            data={"batch_id": "batch-1", "iteration": 1, "artifact_ref": "artifact://validation/1"},
        ).status_code == 201
        for review_number in range(1, 6):
            done = review_batch(
                client,
                decision="failed",
                review_id=f"review-failed-{review_number}",
            )
            assert done.status_code == 201
    else:
        done = worker_block(
            client,
            token,
            block_type="batch_done",
            status=outcome,
            data={
                "batch_id": "batch-1",
                "outcome": outcome,
                "artifact_ref": "workflow-42",
                "capabilities": ["notifications", "email"],
                "eval_score": 9,
            },
        )
    if outcome == "succeeded":
        done = worker_block(
            client,
            token,
            block_type="batch_done",
            status=outcome,
            data={
                "batch_id": "batch-1",
                "outcome": outcome,
                "artifact_ref": "workflow-42",
                "capabilities": ["notifications", "email"],
                "eval_score": 9,
            },
        )
    assert done.status_code == 201

    detail = client.get("/batches/batch-1")
    assert detail.status_code == 200
    assert detail.json()["status"] == outcome
    assert client.get("/batches", params={"status": outcome}).json() == [
        {"batch_id": "batch-1", "title": "Build the notification workflow"}
    ]
    assert worker_block(client, token, block_type="validation_run").status_code == 409
    assert client.post("/batches/batch-1/claim").status_code == 409


def test_succeeded_requires_current_iteration_validation_run(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)

    rejected = worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "succeeded"},
    )
    assert rejected.status_code == 422
    assert all(block["block_type"] != "batch_done" for block in client.get("/batches/batch-1/blocks").json())

    assert worker_block(
        client,
        token,
        block_type="validation_run",
        data={"batch_id": "batch-1", "iteration": 1, "artifact_ref": "artifact://validation/1"},
    ).status_code == 201
    missing_review = worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "succeeded"},
    )
    assert missing_review.status_code == 422
    assert review_batch(client).status_code == 201
    assert worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "succeeded"},
    ).status_code == 201


def test_prior_iteration_validation_cannot_authorize_success(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import store as store_module

    start = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store_module, "_utcnow", lambda: start)
    create_batch(client)
    first_token = claim(client)
    assert worker_block(
        client,
        first_token,
        block_type="validation_run",
        data={"batch_id": "batch-1", "iteration": 1},
    ).status_code == 201

    monkeypatch.setattr(store_module, "_utcnow", lambda: start + timedelta(minutes=91))
    second_token = claim(client)
    assert worker_block(
        client,
        second_token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "succeeded"},
    ).status_code == 422
    assert worker_block(
        client,
        second_token,
        block_type="validation_run",
        data={"batch_id": "batch-1", "iteration": 2, "artifact_ref": "artifact://validation/2"},
    ).status_code == 201
    assert review_batch(client, iteration=2).status_code == 201
    assert worker_block(
        client,
        second_token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "succeeded"},
    ).status_code == 201


def test_batch_done_status_must_match_its_outcome(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)

    response = worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={"batch_id": "batch-1", "outcome": "failed"},
    )

    assert response.status_code == 422


def test_mirror_failure_never_fails_a_ledger_write(storage: MariaDBStorage) -> None:
    client = TestClient(legacy_application(storage=storage, mirror=RecordingMirror(fail=True)))

    response = client.post(
        "/blocks",
        json={"block_type": "work_batch", "data": batch_payload(title="T")},
    )

    assert response.status_code == 201
    assert storage.load()[0]["data"]["batch_id"] == "batch-1"


def test_heartbeat_extends_a_live_claim(client: TestClient, storage: MariaDBStorage) -> None:
    parent_index = create_batch(client)
    parent_before = storage.load()[0]
    token = claim(client)

    response = client.post(
        "/batches/batch-1/claim/heartbeat",
        headers={"X-Claim-Token": token},
    )

    assert response.status_code == 200
    expiry = datetime.fromisoformat(response.json()["claim_expires_at"])
    assert timedelta(minutes=29) < expiry - datetime.now(timezone.utc) <= timedelta(minutes=30)
    blocks = storage.load()
    assert next(block for block in blocks if block["index"] == parent_index) == parent_before
    lifecycle = [block for block in blocks if block["parent_index"] == parent_index]
    assert [block["block_type"] for block in lifecycle] == ["batch_claimed", "batch_heartbeat"]
    assert set(lifecycle[-1]["data"]) == {"batch_id", "claim_expires_at"}
    assert datetime.fromisoformat(lifecycle[-1]["data"]["claim_expires_at"]) == expiry


def test_expired_claim_is_lazily_reclaimable_and_lists_as_pending(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from gateway import store as store_module

    start = datetime(2026, 7, 16, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(store_module, "_utcnow", lambda: start)
    create_batch(client)
    old_token = claim(client)
    monkeypatch.setattr(store_module, "_utcnow", lambda: start + timedelta(minutes=91))

    assert client.get("/batches/batch-1").json()["status"] == "pending"
    assert client.get("/batches", params={"status": "pending"}).json() == [
        {"batch_id": "batch-1", "title": "Build the notification workflow"}
    ]

    new_token = claim(client)

    assert new_token != old_token
    assert client.get("/batches/batch-1").json()["claim_iteration"] == 2
    assert worker_block(client, old_token).status_code == 409
    assert worker_block(
        client,
        new_token,
        data={"batch_id": "batch-1", "iteration": 2},
    ).status_code == 201


def test_approve_is_flag_gated_and_transitions_pending_review(storage: MariaDBStorage) -> None:
    disabled = TestClient(
        legacy_application(storage=storage, mirror=RecordingMirror(), approval_enabled=False)
    )
    create_batch(disabled, status="pending_review")
    assert disabled.post("/batches/batch-1/claim").status_code == 409
    assert disabled.post("/batches/batch-1/approve").status_code == 404

    enabled = TestClient(
        legacy_application(storage=storage, mirror=RecordingMirror(), approval_enabled=True)
    )
    assert enabled.post("/batches/batch-1/approve").status_code == 200
    assert enabled.get("/batches", params={"status": "pending"}).json() == [
        {"batch_id": "batch-1", "title": "Build the notification workflow"}
    ]
    blocks = storage.load()
    parent = next(block for block in blocks if block["block_type"] == "work_batch")
    approval = next(block for block in blocks if block["block_type"] == "batch_approved")
    assert parent["status"] == "pending_review"
    assert parent["metadata"] == {}
    assert parent["children"] == []
    assert approval["parent_index"] == parent["index"]
    assert approval["data"] == {"batch_id": "batch-1"}


def test_mock_crm_sink_round_trip(client: TestClient) -> None:
    payload = {"case_id": "case-1", "tag": "qualified", "account": "acme"}

    assert client.post("/sink/crm", json=payload).status_code == 201
    assert client.get("/sink/crm", params={"case_id": "case-1"}).json() == [payload]


def test_successful_batch_is_searchable_as_validated_capability(client: TestClient) -> None:
    create_batch(client)
    token = claim(client)
    assert client.get("/capabilities", params={"need": "email"}).json() == []
    assert worker_block(
        client,
        token,
        block_type="validation_run",
        data={"batch_id": "batch-1", "iteration": 1, "artifact_ref": "artifact://validation/1"},
    ).status_code == 201
    assert review_batch(client).status_code == 201
    assert worker_block(
        client,
        token,
        block_type="batch_done",
        status="succeeded",
        data={
            "batch_id": "batch-1",
            "outcome": "succeeded",
            "artifact_ref": "workflow-42",
            "capabilities": ["notifications", "email"],
            "eval_score": 9,
        },
    ).status_code == 201

    response = client.get("/capabilities", params={"need": "email"})

    assert response.status_code == 200
    assert response.json()[0]["batch_id"] == "batch-1"
    assert response.json()[0]["artifact_ref"] == "workflow-42"


def test_all_write_routes_are_async_and_uvicorn_is_single_worker(storage: MariaDBStorage) -> None:
    app = create_app(storage=storage, mirror=RecordingMirror())
    write_routes = [
        route for route in app.routes
        if getattr(route, "methods", set()) & {"POST", "PUT", "PATCH", "DELETE"}
    ]

    assert write_routes
    assert all(inspect.iscoroutinefunction(route.endpoint) for route in write_routes)
    source = open("gateway/app.py", encoding="utf-8").read()
    assert 'workers=1' in source


DELIVERY_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def delivery_event(
    event_id: int,
    *,
    project_id: str = "project-1",
    run_id: str = "run-1",
    fencing_token: int | None = 1,
    e2e_run_id: str | None = None,
) -> DeliveryEventEnvelope:
    trace: dict[str, Any] = {
        "project_id": project_id,
        "run_id": run_id,
        "trace_id": f"trace-{event_id}",
        "batch_id": "batch-1",
        "claim_id": "claim-1",
        "fencing_token": fencing_token,
    }
    if e2e_run_id is None:
        return DeliveryEventEnvelope.model_validate(
            {
                "event_id": UUID(int=event_id),
                "event_type": "codex_task",
                "occurred_at": DELIVERY_NOW + timedelta(minutes=event_id),
                "actor": "captain",
                "trace": trace,
                "payload": {
                    "event_type": "codex_task",
                    "task_id": f"task-{event_id}",
                    "target": "n8n",
                    "context_sha256": "a" * 64,
                    "workspace_ref": f"artifact://workspaces/{event_id}",
                    "permissions": ["filesystem.read"],
                    "budget": 100,
                },
            }
        )
    return DeliveryEventEnvelope.model_validate(
        {
            "event_id": UUID(int=event_id),
            "event_type": "e2e_run",
            "occurred_at": DELIVERY_NOW + timedelta(minutes=event_id),
            "actor": "evaluator",
            "trace": trace,
            "payload": {
                "event_type": "e2e_run",
                "e2e_run_id": e2e_run_id,
                "run_index": event_id,
                "clean": True,
                "trace_complete": True,
                "evidence_refs": [f"artifact://evidence/{e2e_run_id}"],
            },
        }
    )


def test_delivery_event_append_is_idempotent_but_conflicting_reuse_is_rejected(
    storage: MariaDBStorage,
) -> None:
    store = GatewayStore(storage)
    event = delivery_event(1)

    created = store.append_delivery_event(event)
    replayed = store.append_delivery_event(event)

    assert created.event == event
    assert created.replayed is False
    assert replayed.event == event
    assert replayed.replayed is True
    assert [block["block_type"] for block in storage.load()] == ["delivery_event"]

    changed = event.model_copy(
        update={"payload": event.payload.model_copy(update={"budget": 101})}
    )
    with pytest.raises(HTTPException, match="event_id already exists with different content") as exc:
        store.append_delivery_event(changed)
    assert exc.value.status_code == 409
    assert len(storage.load()) == 1


def test_delivery_history_is_append_only_filtered_and_rejects_stale_fencing(
    storage: MariaDBStorage,
) -> None:
    store = GatewayStore(storage)
    first = delivery_event(1, fencing_token=2)
    other_run = delivery_event(2, run_id="run-2", fencing_token=1)
    stale = delivery_event(3, fencing_token=1)

    store.append_delivery_event(first)
    store.append_delivery_event(other_run)
    with pytest.raises(HTTPException, match="stale fencing token") as exc:
        store.append_delivery_event(stale)

    assert exc.value.status_code == 409
    assert store.delivery_events(project_id="project-1", run_id="run-1") == (first,)
    assert store.delivery_events(project_id="project-1", run_id="missing") == ()


def test_delivery_api_returns_replay_history_and_stored_release_projection(
    client: TestClient,
) -> None:
    events = [delivery_event(index, e2e_run_id=f"e2e-{index}") for index in range(1, 4)]

    first = client.post("/v1/delivery/events", json=events[0].model_dump(mode="json"))
    replay = client.post("/v1/delivery/events", json=events[0].model_dump(mode="json"))
    for event in events[1:]:
        response = client.post("/v1/delivery/events", json=event.model_dump(mode="json"))
        assert response.status_code == 201, response.text

    assert first.status_code == 201
    assert first.json()["replayed"] is False
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    history = client.get("/v1/projects/project-1/runs/run-1/events")
    assert history.status_code == 200
    assert [item["event_id"] for item in history.json()] == [str(event.event_id) for event in events]
    release = client.get("/v1/projects/project-1/runs/run-1/release")
    assert release.status_code == 200
    assert release.json() == ReleaseProjection(
        status="ready",
        clean_e2e_run_ids=("e2e-1", "e2e-2", "e2e-3"),
        missing_clean_e2e_runs=0,
    ).model_dump(mode="json")


def test_delivery_event_requires_current_gateway_claim_identity_atomically(
    storage: MariaDBStorage,
) -> None:
    store = GatewayStore(storage)
    store.append(
        BlockRequest(
            block_type="work_batch",
            status="pending",
            data=batch_payload(),
        ),
        claim_token=None,
    )
    claim = store.claim("batch-1")
    current = delivery_event(900).model_copy(
        update={
            "trace": delivery_event(900).trace.model_copy(
                update={
                    "claim_id": claim["claim_id"],
                    "fencing_token": claim["fencing_token"],
                }
            )
        }
    )

    assert store.append_delivery_event(
        current, require_current_claim=True
    ).event == current

    wrong_claim = current.model_copy(
        update={
            "event_id": UUID(int=901),
            "trace": current.trace.model_copy(update={"claim_id": "wrong-claim"}),
        }
    )
    with pytest.raises(HTTPException, match="current claim") as claim_error:
        store.append_delivery_event(wrong_claim, require_current_claim=True)
    assert claim_error.value.status_code == 409

    stale_fence = current.model_copy(
        update={
            "event_id": UUID(int=902),
            "trace": current.trace.model_copy(
                update={"fencing_token": claim["fencing_token"] - 1}
            ),
        }
    )
    with pytest.raises(HTTPException, match="current claim") as fence_error:
        store.append_delivery_event(stale_fence, require_current_claim=True)
    assert fence_error.value.status_code == 409
