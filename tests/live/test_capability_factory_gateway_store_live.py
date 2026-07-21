"""Explicit, destructive captain_test proof for the capability factory authority chain."""

from __future__ import annotations

import os
from datetime import timedelta
from urllib.parse import urlsplit
from uuid import UUID, uuid5

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


def test_recovered_claim_records_fresh_provider_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dsn = _explicit_isolated_test_dsn()
    from fastapi import HTTPException

    from agenten.agent_factory.capability_factory_entrypoint import (
        ProviderEffectReceipt,
    )
    from agenten.agent_runtime.contracts import AgentRuntimeResult, ArtifactRef
    from blockchain.mariadb_storage import MariaDBStorage
    from gateway.contracts import (
        RuntimeExecutionClaimRequest,
        canonical_contract_sha256,
    )
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
        GatewayStore(
            storage,
            clock=lambda: gateway_acceptance.TEST_GATEWAY_NOW,
        ).publish_capability_release(release)
        command_payload, result_payload = (
            gateway_acceptance._seed_runtime_command_and_grant(storage)
        )
        command_id = UUID(str(command_payload["event_id"]))
        claim_request = RuntimeExecutionClaimRequest(
            schema_name="captain.runtime-execution-claim-request.v1",
            command_id=command_id,
            owner_id="runtime-a",
            lease_seconds=60,
            capability_id=release.package.capability_id,
            capability_version=release.package.capability_version,
        )
        first = GatewayStore(
            storage,
            clock=lambda: gateway_acceptance.TEST_GATEWAY_NOW
            + timedelta(seconds=20),
        ).claim_runtime_execution(claim_request)
        recovered = GatewayStore(
            storage,
            clock=lambda: gateway_acceptance.TEST_GATEWAY_NOW
            + timedelta(seconds=81),
        ).claim_runtime_execution(claim_request)
        assert first.claim.fencing_token == 1
        assert recovered.claim.fencing_token == 2
        assert recovered.claim_credential is not None

        provider_result = AgentRuntimeResult.model_validate(result_payload)
        effect_id = uuid5(command_id, "durable-provider-effect")
        provider_receipt = ProviderEffectReceipt(
            provider_operation_id=f"provider-operation:{effect_id}",
            effect_id=effect_id,
            command_id=command_id,
            request_digest="a" * 64,
            result_digest=canonical_contract_sha256(provider_result),
            status=provider_result.status.value,
            idempotency_guaranteed=True,
        )
        receipt_digest = canonical_contract_sha256(provider_receipt)
        receipt_ref = ArtifactRef(
            uri=f"artifact://provider-effect-receipt/{receipt_digest}",
            sha256=receipt_digest,
            media_type="application/json",
        )
        observation = provider_result.model_copy(
            update={
                "occurred_at": recovered.claim.claimed_at,
                "evidence_refs": (*provider_result.evidence_refs, receipt_ref),
            }
        )
        completion_store = GatewayStore(
            storage,
            clock=lambda: gateway_acceptance.TEST_GATEWAY_NOW
            + timedelta(seconds=90),
        )
        with pytest.raises(HTTPException, match="lease window"):
            completion_store.record_runtime_result(
                provider_result,
                execution_owner_id=recovered.claim.owner_id,
                execution_fencing_token=recovered.claim.fencing_token,
                execution_claim_credential=recovered.claim_credential,
            )

        result_receipt = completion_store.record_runtime_result(
            observation,
            execution_owner_id=recovered.claim.owner_id,
            execution_fencing_token=recovered.claim.fencing_token,
            execution_claim_credential=recovered.claim_credential,
        )
        execution = gateway_acceptance._execution_request(
            release,
            command_payload,
            observation.model_dump(mode="json", by_alias=True),
        ).model_copy(
            update={"claim_fencing_token": recovered.claim.fencing_token}
        )
        execution_receipt = completion_store.record_capability_execution(execution)
        persisted_result = completion_store.runtime_operation(command_id).result
        persisted_claim = completion_store.runtime_execution_claim(command_id)

        assert result_receipt.replayed is False
        assert execution_receipt.replayed is False
        assert persisted_result == observation
        assert persisted_claim is not None
        assert persisted_claim.status == "completed"
        assert persisted_claim.fencing_token == 2
        assert (
            persisted_claim.claimed_at
            <= observation.occurred_at
            < persisted_claim.expires_at
        )
        assert provider_receipt.result_digest == canonical_contract_sha256(
            provider_result
        )
        assert receipt_ref in observation.evidence_refs
    finally:
        storage.clear()
