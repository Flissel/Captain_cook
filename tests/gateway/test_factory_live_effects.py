from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5

import pytest

from tests.agent_factory.test_factory_live_runner import effect_outcome, effect_request
from tests.agent_factory.test_release_gate import workflow_job
from tests.gateway.test_factory_budget import record_usage_lease
from tests.support.mariadb import assert_isolated_test_database

from agenten.agent_factory.factory_live_runner import (
    FactoryLiveEffectClaim,
    FactoryLiveEffectRecord,
    FactoryLiveEffectWriteReceipt,
)
from blockchain.mariadb_storage import MariaDBStorage
from gateway.contracts import FactorySkillAssignmentV1
from gateway.factory_repository import GatewayFactoryLiveEffectLedger
from gateway.store import GatewayStore


TEST_DSN = os.getenv("TEST_MARIADB_DSN")


class PersistentEffectStore:
    """Store-shaped fake whose state outlives Gateway adapter instances."""

    def __init__(self) -> None:
        self.records = {}

    def claim_factory_live_effect(self, request):
        existing = self.records.get(request.effect_id)
        if existing is not None:
            if existing.request != request:
                raise ValueError("conflicting effect claim")
            return FactoryLiveEffectClaim(record=existing, acquired=False)
        record = FactoryLiveEffectRecord(request=request)
        self.records[request.effect_id] = record
        return FactoryLiveEffectClaim(record=record, acquired=True)

    def complete_factory_live_effect(self, request, outcome):
        existing = self.records[request.effect_id]
        if existing.outcome is not None:
            if existing.outcome != outcome:
                raise ValueError("conflicting effect completion")
            return FactoryLiveEffectWriteReceipt(record=existing, replayed=True)
        record = FactoryLiveEffectRecord(request=request, outcome=outcome)
        self.records[request.effect_id] = record
        return FactoryLiveEffectWriteReceipt(record=record, replayed=False)


def test_gateway_effect_claim_and_completion_survive_adapter_restart() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    outcome = effect_outcome(request)
    store = PersistentEffectStore()

    first = GatewayFactoryLiveEffectLedger(store).claim(request)
    after_reservation = GatewayFactoryLiveEffectLedger(store).claim(request)
    completed = GatewayFactoryLiveEffectLedger(store).complete(request, outcome)
    after_evidence = GatewayFactoryLiveEffectLedger(store).claim(request)

    assert first.acquired is True
    assert after_reservation.acquired is False
    assert after_reservation.record.outcome is None
    assert completed.replayed is False
    assert after_evidence.acquired is False
    assert after_evidence.record.outcome == outcome


def test_gateway_effect_adapter_translates_store_conflicts() -> None:
    job = workflow_job(mode="demo")
    request = effect_request(job)
    store = PersistentEffectStore()
    adapter = GatewayFactoryLiveEffectLedger(store)
    adapter.claim(request)

    changed = request.model_copy(update={"idempotency_key": "c" * 64})
    try:
        adapter.claim(changed)
    except ValueError as exc:
        assert "conflicting effect claim" in str(exc)
    else:
        raise AssertionError("changed claim replay must fail")


@pytest.fixture
def mariadb_store() -> GatewayStore:
    if TEST_DSN is None:
        pytest.skip("TEST_MARIADB_DSN is not configured")
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    store = GatewayStore(storage)
    storage.clear()
    yield store
    storage.clear()


def prepared_mariadb_effect(store: GatewayStore):
    now = datetime.now(timezone.utc)
    template = workflow_job(mode="demo")
    job = template.model_copy(
        update={
            "occurred_at": now,
            "deadline_at": now + timedelta(minutes=15),
        }
    )
    store.record_factory_job(job)
    lease = record_usage_lease(store, job)
    request = effect_request(job, now=now)
    invocation = request.invocation.model_copy(
        update={
            "lease": lease,
            "input_ref": job.input_ref,
            "input_sha256": job.input_ref.sha256,
        }
    )
    request = request.model_copy(
        update={"input_ref": job.input_ref, "invocation": invocation}
    )
    store.record_released_factory_skill(invocation.released_skill)
    store.record_factory_skill_assignment(
        FactorySkillAssignmentV1(
            job_id=job.job_id,
            step=invocation.step,
            released_skill=invocation.released_skill,
        )
    )
    return job, request


def test_mariadb_effect_claim_is_atomic_and_restart_safe(
    mariadb_store: GatewayStore,
) -> None:
    _, request = prepared_mariadb_effect(mariadb_store)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = tuple(
            pool.map(
                lambda _: GatewayFactoryLiveEffectLedger(mariadb_store).claim(request),
                range(2),
            )
        )
    assert sorted(claim.acquired for claim in claims) == [False, True]

    restarted = GatewayStore(mariadb_store.storage)
    replay = GatewayFactoryLiveEffectLedger(restarted).claim(request)
    assert replay.acquired is False
    assert replay.record.outcome is None

    outcome = effect_outcome(request)
    GatewayFactoryLiveEffectLedger(restarted).complete(request, outcome)
    recovered = GatewayFactoryLiveEffectLedger(
        GatewayStore(mariadb_store.storage)
    ).claim(request)
    assert recovered.acquired is False
    assert recovered.record.outcome == outcome


def test_mariadb_effect_changed_replay_conflicts(
    mariadb_store: GatewayStore,
) -> None:
    _, request = prepared_mariadb_effect(mariadb_store)
    adapter = GatewayFactoryLiveEffectLedger(mariadb_store)
    adapter.claim(request)
    changed = request.model_copy(update={"effect_id": request.invocation.invocation_id})

    with pytest.raises(ValueError, match="different content"):
        adapter.claim(changed)


def test_mariadb_effect_migration_binds_changed_claim_to_existing_invocation(
    mariadb_store: GatewayStore,
) -> None:
    job, request = prepared_mariadb_effect(mariadb_store)
    GatewayFactoryLiveEffectLedger(mariadb_store).claim(request)

    with mariadb_store.storage.transaction() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SHOW INDEX FROM factory_live_effect_events "
                "WHERE Key_name = 'uq_factory_live_effect_invocation'"
            )
            if cursor.fetchone() is not None:
                cursor.execute(
                    "ALTER TABLE factory_live_effect_events "
                    "DROP INDEX uq_factory_live_effect_invocation"
                )
            cursor.execute(
                "SHOW COLUMNS FROM factory_live_effect_events LIKE 'invocation_id'"
            )
            if cursor.fetchone() is not None:
                cursor.execute(
                    "ALTER TABLE factory_live_effect_events DROP COLUMN invocation_id"
                )

    restarted = GatewayStore(mariadb_store.storage)
    changed_input = job.compiled_spec_ref
    changed_invocation = request.invocation.model_copy(
        update={
            "idempotency_key": "e" * 64,
            "input_ref": changed_input,
            "input_sha256": changed_input.sha256,
        }
    )
    changed = request.model_copy(
        update={
            "effect_id": uuid5(NAMESPACE_URL, "changed-mariadb-invocation-effect-id"),
            "idempotency_key": changed_invocation.idempotency_key,
            "input_ref": changed_input,
            "invocation": changed_invocation,
        }
    )

    replay = GatewayFactoryLiveEffectLedger(restarted).claim(request)
    assert replay.acquired is False
    with pytest.raises(ValueError, match="different content"):
        GatewayFactoryLiveEffectLedger(restarted).claim(changed)
