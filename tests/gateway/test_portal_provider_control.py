from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.agent_runtime.contracts import ArtifactRef
from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from blockchain.mariadb_storage import MariaDBStorage
from gateway.integration_setup_contracts import IntegrationSetupSubmissionV1
from gateway.portal_live_contracts import (
    PortalProviderAuditQueryV1,
    PortalProviderProbeCompletionV1,
    PortalProviderProbeRequestV1,
)
from gateway.store import GatewayStore
from gateway.app import create_app
from gateway.settings import GatewaySettings
from agenten.agent_factory.integration_setup import CredentialVerificationReceiptV1
from tests.agent_factory.test_state_machine import job
from tests.gateway.test_integration_setup_api import ready_setup_payload
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
NOW = datetime(2026, 8, 5, 16, tzinfo=timezone.utc)


def _ref(name: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _release(digest: str) -> GiteaTemplateReleaseV1:
    revision = "1" * 40
    return GiteaTemplateReleaseV1(
        repository="captain/templates",
        revision=revision,
        path="verification/bearer.json",
        contents_url=(
            f"https://gitea.example/captain/templates/raw/commit/{revision}/"
            "verification/bearer.json"
        ),
        sha256=digest,
    )


class RecordingProviderSource:
    def __init__(self) -> None:
        self.calls = 0

    def verify_credential(self, **kwargs):
        self.calls += 1
        requirement = kwargs["requirement"]
        credential = kwargs["credential"]
        return CredentialVerificationReceiptV1(
            integration_key=requirement.integration_key,
            credential_alias=requirement.credential_alias,
            credential_id=credential.credential_id,
            credential_type=credential.credential_type,
            project_id=credential.project_id,
            status="passed",
            occurred_at=kwargs["now"],
            template_ref=_ref("gitea", "a" * 64),
            verification_release=_release("a" * 64),
            template_content_sha256="a" * 64,
            workflow_ref=_ref("n8n-workflow", "c" * 64),
            workflow_content_sha256="c" * 64,
            execution_ref=_ref("n8n-execution", "d" * 64),
        )


class NullMirror:
    def enqueue_nowait(self, block):
        del block


def test_probe_start_and_completion_are_append_only_idempotent_and_auditable() -> None:
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
    request = PortalProviderProbeRequestV1(
        probe_request_id=UUID("30000000-0000-4000-8000-000000000001"),
        run_id="portal-live-v1",
        job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        integration_kind="bearer",
        credential_alias="CRM_API_KEY",
        credential_id="cred-prod",
        setup_revision=setup.revision,
        setup_content_sha256=persisted.content_sha256,
        verification_template_sha256="a" * 64,
    )

    try:
        started = store.record_portal_provider_probe_start(request, occurred_at=NOW)
        replayed = store.record_portal_provider_probe_start(request, occurred_at=NOW)
        assert started.status == "started"
        assert started.replayed is False
        assert replayed.replayed is True

        with pytest.raises(HTTPException) as conflict:
            store.record_portal_provider_probe_start(
                request.model_copy(update={"credential_id": "foreign-credential"}),
                occurred_at=NOW,
            )
        assert conflict.value.status_code == 409

        completion = PortalProviderProbeCompletionV1(
            probe_request_id=request.probe_request_id,
            trace_id=UUID("40000000-0000-4000-8000-000000000001"),
            run_id=request.run_id,
            job_id=request.job_id,
            correlation_id=request.correlation_id,
            integration_kind=request.integration_kind,
            credential_alias=request.credential_alias,
            credential_id=request.credential_id,
            setup_revision=request.setup_revision,
            setup_content_sha256=request.setup_content_sha256,
            template_ref=_ref("gitea", "a" * 64),
            template_release=_release("a" * 64),
            deployed_workflow_ref=_ref("n8n-workflow", "c" * 64),
            execution_ref=_ref("n8n-execution", "d" * 64),
            status="passed",
            occurred_at=NOW + timedelta(seconds=1),
        )
        completed = store.record_portal_provider_probe_completion(completion)
        completed_replay = store.record_portal_provider_probe_completion(completion)
        assert completed.status == "passed"
        assert completed.replayed is False
        assert completed_replay.replayed is True

        audit = store.portal_provider_audit(
            PortalProviderAuditQueryV1(
                run_id=request.run_id,
                job_id=request.job_id,
                correlation_id=request.correlation_id,
            ),
            observed_at=NOW + timedelta(seconds=2),
        )
        assert audit.invocation_count == 1
        assert audit.completion_count == 1
        assert audit.trace_ids == (completion.trace_id,)
        assert [
            block["block_type"]
            for block in storage.load()
            if block["block_type"].startswith("portal_provider_probe_")
        ] == ["portal_provider_probe_started", "portal_provider_probe_completed"]
    finally:
        storage.clear()


def test_provider_probe_orchestration_never_repeats_a_completed_effect() -> None:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    storage.clear()
    provider = RecordingProviderSource()
    store = GatewayStore(storage, portal_verification_source=provider)
    factory_job = job()
    store.record_factory_job(factory_job)
    setup = IntegrationSetupSubmissionV1.model_validate(ready_setup_payload(factory_job))
    store.record_integration_setup(setup)
    persisted = store.integration_setup(factory_job.job_id)
    request = PortalProviderProbeRequestV1(
        probe_request_id=UUID("30000000-0000-4000-8000-000000000010"),
        run_id="portal-live-v1",
        job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        integration_kind="bearer",
        credential_alias="CRM_API_KEY",
        credential_id="cred-prod",
        setup_revision=setup.revision,
        setup_content_sha256=persisted.content_sha256,
        verification_template_sha256="a" * 64,
    )

    try:
        first = store.run_portal_provider_probe(request, now=NOW)
        replay = store.run_portal_provider_probe(request, now=NOW + timedelta(seconds=2))
        assert first == replay
        assert provider.calls == 1
    finally:
        storage.clear()


def test_provider_control_routes_require_only_the_provider_capability() -> None:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    storage.clear()
    provider = RecordingProviderSource()
    store = GatewayStore(storage, portal_verification_source=provider)
    factory_job = job()
    store.record_factory_job(factory_job)
    setup = IntegrationSetupSubmissionV1.model_validate(ready_setup_payload(factory_job))
    store.record_integration_setup(setup)
    persisted = store.integration_setup(factory_job.job_id)
    request = PortalProviderProbeRequestV1(
        probe_request_id=UUID("30000000-0000-4000-8000-000000000020"),
        run_id="portal-live-v1",
        job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        integration_kind="bearer",
        credential_alias="CRM_API_KEY",
        credential_id="cred-prod",
        setup_revision=setup.revision,
        setup_content_sha256=persisted.content_sha256,
        verification_template_sha256="a" * 64,
    )
    settings = GatewaySettings(
        ledger_dsn=SecretStr(TEST_DSN),
        captain_gateway_token=SecretStr("captain-token"),
        worker_gateway_token=SecretStr("worker-token"),
        portal_provider_control_token=SecretStr("provider-token"),
        portal_evidence_token=SecretStr("evidence-token"),
        portal_restart_control_token=SecretStr("restart-token"),
    )

    try:
        with TestClient(
            create_app(
                gateway_store=store,
                settings=settings,
                mirror=NullMirror(),
                portal_clock=lambda: NOW,
            )
        ) as client:
            denied = client.get(
                "/v1/control/provider/health",
                headers={"Authorization": "Bearer evidence-token"},
            )
            health = client.get(
                "/v1/control/provider/health",
                headers={"Authorization": "Bearer provider-token"},
            )
            completed = client.post(
                "/v1/control/provider/probes",
                headers={"Authorization": "Bearer provider-token"},
                json=request.model_dump(mode="json"),
            )
            audit = client.post(
                "/v1/control/provider/audit",
                headers={"Authorization": "Bearer provider-token"},
                json={
                    "run_id": request.run_id,
                    "job_id": str(request.job_id),
                    "correlation_id": str(request.correlation_id),
                },
            )

        assert denied.status_code == 401
        assert health.status_code == 200
        assert set(health.json()) == {"status", "service_version", "boot_id"}
        assert completed.status_code == 200
        assert completed.json()["credential_id"] == "cred-prod"
        assert audit.status_code == 200
        assert audit.json()["invocation_count"] == 1
        assert audit.json()["completion_count"] == 1
    finally:
        storage.clear()
