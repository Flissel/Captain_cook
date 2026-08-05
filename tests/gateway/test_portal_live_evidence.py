from __future__ import annotations

import hashlib
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from agenten.agent_factory.gitea_template_contracts import GiteaTemplateReleaseV1
from agenten.agent_factory.integration_setup import (
    CredentialVerificationReceiptV1,
    IntegrationConnectionV1,
    IntegrationCredentialRequirementV1,
    IntegrationSetupStatus,
    N8nCredentialMetadataV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    MinibookProjectionRebuildReceiptV1,
    minibook_projection_acknowledgement_id,
)
from agenten.delivery.projector import MinibookProjector
from blockchain.mariadb_storage import MariaDBStorage
from gateway.integration_setup_contracts import IntegrationSetupSubmissionV1
from gateway.app import create_app
from gateway.portal_live_contracts import (
    PortalLiveEvidenceQueryV1,
    PortalLiveRunFinalizationV1,
    PortalProviderProbeCompletionV1,
    PortalProviderProbeRequestV1,
    PortalRestartReceiptV1,
)
from gateway.registry_feed import integration_setup_projection
from gateway.store import GatewayStore
from gateway.settings import GatewaySettings
from tests.agent_factory.test_state_machine import job
from tests.gateway.test_integration_setup_api import ready_setup_payload
from tests.support.mariadb import assert_isolated_test_database


TEST_DSN = os.getenv("TEST_MARIADB_DSN")
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="TEST_MARIADB_DSN is not configured")
NOW = datetime(2026, 8, 5, 18, tzinfo=timezone.utc)


class NullMirror:
    def enqueue_nowait(self, block):
        del block


def _ref(name: str, digest: str) -> ArtifactRef:
    return ArtifactRef(
        uri=f"artifact://{name}/{digest}",
        sha256=digest,
        media_type="application/json",
    )


def _release(kind: str, digest: str) -> GiteaTemplateReleaseV1:
    revision = ("1" if kind == "bearer" else "2") * 40
    return GiteaTemplateReleaseV1(
        repository="captain/templates",
        revision=revision,
        path=f"verification/{kind}.json",
        contents_url=(
            f"https://gitea.example/captain/templates/raw/commit/{revision}/"
            f"verification/{kind}.json"
        ),
        sha256=digest,
    )


def _setup_with_bearer_and_oauth(factory_job) -> IntegrationSetupSubmissionV1:
    setup = IntegrationSetupSubmissionV1.model_validate(ready_setup_payload(factory_job))
    oauth_requirement = IntegrationCredentialRequirementV1(
        integration_key="crm",
        credential_alias="CRM_OAUTH",
        credential_type="oAuth2Api",
        required=True,
        setup_method="n8n_ui",
        setup_label="CRM OAuth",
        project_id="captain-production",
        verification_workflow_sha256="b" * 64,
    )
    oauth_credential = N8nCredentialMetadataV1(
        credential_id="cred-oauth",
        credential_name="CRM OAuth production",
        credential_type="oAuth2Api",
        project_id="captain-production",
    )
    oauth_receipt = CredentialVerificationReceiptV1(
        integration_key="crm",
        credential_alias="CRM_OAUTH",
        credential_id="cred-oauth",
        credential_type="oAuth2Api",
        project_id="captain-production",
        status="passed",
        occurred_at=NOW - timedelta(minutes=1),
        template_ref=_ref("gitea", "b" * 64),
        verification_release=_release("oauth", "b" * 64),
        template_content_sha256="b" * 64,
        workflow_ref=_ref("n8n-workflow", "c" * 64),
        workflow_content_sha256="c" * 64,
        execution_ref=_ref("n8n-execution", "d" * 64),
        oauth_consent_ref=_ref("oauth-consent", "e" * 64),
        oauth_callback_ref=_ref("oauth-callback", "f" * 64),
    )
    oauth_connection = IntegrationConnectionV1(
        requirement=oauth_requirement,
        status=IntegrationSetupStatus.READY,
        candidate_credentials=(oauth_credential,),
        selected_credential=oauth_credential,
        verification_receipt=oauth_receipt,
    )
    return setup.model_copy(
        update={
            "plan": setup.plan.model_copy(
                update={"connections": (*setup.plan.connections, oauth_connection)}
            )
        }
    )


def test_finalization_is_atomic_and_evidence_query_is_read_only() -> None:
    assert TEST_DSN is not None
    assert_isolated_test_database(TEST_DSN)
    storage = MariaDBStorage(TEST_DSN)
    storage.clear()
    store = GatewayStore(storage)
    factory_job = job()
    store.record_factory_job(factory_job)
    setup = _setup_with_bearer_and_oauth(factory_job)
    store.record_integration_setup(setup)
    persisted = store.integration_setup(factory_job.job_id)
    run_id = "portal-live-v1"
    traces: list[PortalProviderProbeCompletionV1] = []
    trace_specs = (
        ("bearer", "CRM_API_KEY", "cred-prod", "a" * 64),
        ("oauth2", "CRM_OAUTH", "cred-oauth", "b" * 64),
        ("bearer", "CRM_API_KEY", "cred-prod", "a" * 64),
    )
    for index, (kind, alias, credential_id, template_sha) in enumerate(trace_specs, 1):
        request = PortalProviderProbeRequestV1(
            probe_request_id=UUID(f"30000000-0000-4000-8000-{index:012d}"),
            run_id=run_id,
            job_id=factory_job.job_id,
            correlation_id=factory_job.correlation_id,
            integration_kind=kind,
            credential_alias=alias,
            credential_id=credential_id,
            setup_revision=setup.revision,
            setup_content_sha256=persisted.content_sha256,
            verification_template_sha256=template_sha,
        )
        store.record_portal_provider_probe_start(
            request,
            occurred_at=NOW + timedelta(seconds=index),
        )
        release_kind = "oauth" if kind == "oauth2" else "bearer"
        completion = PortalProviderProbeCompletionV1(
            probe_request_id=request.probe_request_id,
            trace_id=UUID(f"40000000-0000-4000-8000-{index:012d}"),
            run_id=run_id,
            job_id=factory_job.job_id,
            correlation_id=factory_job.correlation_id,
            integration_kind=kind,
            credential_alias=alias,
            credential_id=credential_id,
            setup_revision=setup.revision,
            setup_content_sha256=persisted.content_sha256,
            template_ref=_ref("gitea", template_sha),
            template_release=_release(release_kind, template_sha),
            deployed_workflow_ref=_ref("n8n-workflow", f"{index + 2:x}" * 64),
            execution_ref=_ref("n8n-execution", f"{index + 5:x}" * 64),
            consent_ref=(None if kind == "bearer" else _ref("oauth-consent", "e" * 64)),
            callback_ref=(None if kind == "bearer" else _ref("oauth-callback", "f" * 64)),
            status="passed",
            occurred_at=NOW + timedelta(seconds=index, microseconds=1),
        )
        store.record_portal_provider_probe_completion(completion)
        traces.append(completion)

    restart = PortalRestartReceiptV1(
        restart_request_id=UUID("50000000-0000-4000-8000-000000000001"),
        restart_id=UUID("60000000-0000-4000-8000-000000000001"),
        run_id=run_id,
        job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        services=("gateway", "portal"),
        previous_gateway_boot_id="boot-before",
        new_gateway_boot_id="boot-after",
        portal_deployment_id="portal-deployment-1",
        setup_revision=setup.revision,
        setup_content_sha256=persisted.content_sha256,
        status="resumed",
        occurred_at=NOW + timedelta(seconds=5),
    )
    store.record_portal_restart_receipt(restart)
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
        acknowledged_at=NOW + timedelta(seconds=6),
        outcome="mirrored",
    )
    store.record_minibook_projection_acknowledgement(acknowledgement)
    rebuild = MinibookProjectionRebuildReceiptV1(
        rebuild_id=UUID("70000000-0000-4000-8000-000000000001"),
        run_id=run_id,
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
        occurred_at=NOW + timedelta(seconds=7),
    )
    store.record_minibook_projection_rebuild_receipt(rebuild)
    request = PortalLiveRunFinalizationV1(
        decision_request_id=UUID("80000000-0000-4000-8000-000000000001"),
        run_id=run_id,
        job_id=factory_job.job_id,
        correlation_id=factory_job.correlation_id,
        provider_trace_ids=tuple(trace.trace_id for trace in traces),
        restart_id=restart.restart_id,
        minibook_rebuild_id=rebuild.rebuild_id,
        policy_version="portal-live-v1",
        occurred_at=NOW + timedelta(seconds=8),
    )

    try:
        decision = store.finalize_portal_live_run(request)
        query = PortalLiveEvidenceQueryV1(
            run_id=run_id,
            job_id=factory_job.job_id,
            correlation_id=factory_job.correlation_id,
        )
        block_count = len(storage.load())
        evidence = store.portal_live_evidence(query)
        replay = store.portal_live_evidence(query)
        assert len(storage.load()) == block_count
        assert evidence == replay
        assert decision.status == "accepted"
        assert len(evidence.provider_traces) == 3
        assert set(trace.integration_kind for trace in evidence.provider_traces) == {
            "bearer",
            "oauth2",
        }
        assert evidence.gateway_execution_ref == decision.gateway_execution_ref
        assert evidence.minibook_rebuild_ref.uri.startswith("artifact://minibook-rebuild/")

        settings = GatewaySettings(
            ledger_dsn=SecretStr(TEST_DSN),
            captain_gateway_token=SecretStr("captain-token"),
            worker_gateway_token=SecretStr("worker-token"),
            portal_provider_control_token=SecretStr("provider-token"),
            portal_evidence_token=SecretStr("evidence-token"),
            portal_restart_control_token=SecretStr("restart-token"),
        )
        with TestClient(
            create_app(gateway_store=store, settings=settings, mirror=NullMirror())
        ) as client:
            denied = client.post(
                "/v1/control/evidence/query",
                headers={"Authorization": "Bearer provider-token"},
                json=query.model_dump(mode="json"),
            )
            health = client.get(
                "/v1/control/evidence/health",
                headers={"Authorization": "Bearer evidence-token"},
            )
            queried = client.post(
                "/v1/control/evidence/query",
                headers={"Authorization": "Bearer evidence-token"},
                json=query.model_dump(mode="json"),
            )
            finalized = client.post(
                f"/v1/control/portal-runs/{run_id}/decisions",
                headers={"Authorization": "Bearer captain-token"},
                json=request.model_dump(mode="json"),
            )

        assert denied.status_code == 401
        assert health.status_code == 200
        assert queried.status_code == 200
        assert queried.json()["status"] == "accepted"
        assert finalized.status_code == 200
        assert finalized.json()["decision_id"] == str(decision.decision_id)
    finally:
        storage.clear()
