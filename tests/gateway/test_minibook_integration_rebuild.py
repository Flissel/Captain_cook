from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionRebuildReceiptV1,
    minibook_projection_acknowledgement_id,
)
from agenten.delivery.projector import MinibookProjector
from blockchain.mariadb_storage import MariaDBStorage
from gateway.integration_setup_contracts import IntegrationSetupSubmissionV1
from gateway.registry_feed import integration_setup_projection
from gateway.store import GatewayStore
from gateway.app import create_app
from gateway.settings import GatewaySettings
from tests.agent_factory.test_state_machine import job
from tests.gateway.test_integration_setup_api import ready_setup_payload
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
NOW = datetime(2026, 8, 5, 17, tzinfo=timezone.utc)


class NullMirror:
    def enqueue_nowait(self, block):
        del block


def test_rebuild_receipt_requires_acknowledged_latest_setup_projection() -> None:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    storage.clear()
    store = GatewayStore(storage)
    factory_job = job()
    store.record_factory_job(factory_job)
    setup = IntegrationSetupSubmissionV1.model_validate(ready_setup_payload(factory_job))
    store.record_integration_setup(setup)
    persisted = store.integration_setup(factory_job.job_id)
    projection = integration_setup_projection(
        setup,
        factory_job.model_dump(mode="json", by_alias=True),
    )
    rendered = MinibookProjector.render(projection)
    post_id = "captain-projection-" + hashlib.sha256(
        str(projection.event_id).encode()
    ).hexdigest()[:32]
    acknowledgement = MinibookProjectionAcknowledgementV1(
        acknowledgement_id=minibook_projection_acknowledgement_id(
            projection.event_id,
            post_id=post_id,
            content_sha256=rendered.content_hash,
        ),
        projection_event_id=projection.event_id,
        correlation_id=projection.correlation_id,
        subject_id=projection.subject_id,
        subject_version=projection.subject_version,
        project_id=MinibookProjector.PROJECTION_PROJECT_ID,
        post_id=post_id,
        content_sha256=rendered.content_hash,
        acknowledged_at=NOW,
        outcome="mirrored",
    )
    receipt = MinibookProjectionRebuildReceiptV1(
        rebuild_id="70000000-0000-4000-8000-000000000001",
        run_id="portal-live-v1",
        job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        projection_event_id=setup.event_id,
        acknowledgement_id=acknowledgement.acknowledgement_id,
        setup_revision=setup.revision,
        setup_content_sha256=persisted.content_sha256,
        feed_sha256="e" * 64,
        event_ids_sha256="f" * 64,
        target_project_id=MinibookProjector.PROJECTION_PROJECT_ID,
        outcome="converged",
        occurred_at=NOW + timedelta(seconds=1),
    )

    try:
        with pytest.raises(HTTPException) as missing_ack:
            store.record_minibook_projection_rebuild_receipt(receipt)
        assert missing_ack.value.status_code == 409

        store.record_minibook_projection_acknowledgement(acknowledgement)
        stored = store.record_minibook_projection_rebuild_receipt(receipt)
        replay = store.record_minibook_projection_rebuild_receipt(receipt)
        assert stored == receipt
        assert replay == receipt
        assert sum(
            block["block_type"] == "minibook_projection_rebuild_completed"
            for block in storage.load()
        ) == 1
        settings = GatewaySettings(
            ledger_dsn=SecretStr(TEST_DSN),
            captain_gateway_token=SecretStr("captain-token"),
            worker_gateway_token=SecretStr("worker-token"),
        )
        with TestClient(
            create_app(gateway_store=store, settings=settings, mirror=NullMirror())
        ) as client:
            denied = client.post(
                "/api/v1/projections/minibook/rebuild-receipts",
                headers={"Authorization": "Bearer worker-token"},
                json=receipt.model_dump(mode="json", by_alias=True),
            )
            accepted = client.post(
                "/api/v1/projections/minibook/rebuild-receipts",
                headers={"Authorization": "Bearer captain-token"},
                json=receipt.model_dump(mode="json", by_alias=True),
            )
        assert denied.status_code == 403
        assert accepted.status_code == 200
        assert accepted.json()["rebuild_id"] == str(receipt.rebuild_id)
    finally:
        storage.clear()
