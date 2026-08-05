from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from uuid import UUID

from agenten.delivery.minibook_events import (
    MinibookProjectionAcknowledgementV1,
    minibook_projection_acknowledgement_id,
)
from agenten.delivery.projector import MinibookProjector
from gateway.integration_setup_contracts import IntegrationSetupSubmissionV1
from gateway.registry_feed import (
    integration_setup_projection,
    integration_setup_registry_mirror_event,
)
from gateway.store import AppendResult, GatewayStore


def test_integration_setup_projection_exposes_only_aggregate_readiness() -> None:
    submission = IntegrationSetupSubmissionV1.model_validate(
        {
            "schema": "captain.integration-setup-submission.v1",
            "event_id": "80000000-0000-4000-8000-000000000001",
            "job_id": "10000000-0000-4000-8000-000000000001",
            "correlation_id": "20000000-0000-4000-8000-000000000001",
            "subject_version": 1,
            "revision": 1,
            "previous_content_sha256": None,
            "occurred_at": "2026-08-04T12:00:00Z",
            "plan": {
                "schema": "captain.integration-setup-plan.v1",
                "connections": [
                    {
                        "schema": "captain.integration-connection.v1",
                        "requirement": {
                            "schema": "captain.integration-credential-requirement.v1",
                            "integration_key": "crm",
                            "credential_alias": "CRM_PRIMARY",
                            "credential_type": "hubspotApi",
                            "required": True,
                            "setup_method": "n8n_ui",
                            "setup_label": "Connect CRM",
                            "project_id": None,
                            "verification_workflow_sha256": "d" * 64,
                        },
                        "status": "missing",
                        "candidate_credentials": [],
                        "selected_credential": None,
                        "verification_receipt": None,
                    }
                ],
            },
        }
    )
    event = integration_setup_projection(
        submission,
        {
            "event_id": "30000000-0000-4000-8000-000000000001",
            "correlation_id": str(submission.correlation_id),
        },
    )

    rendered = event.model_dump(mode="json", by_alias=True, exclude_none=True)
    assert event.event_id == UUID("80000000-0000-4000-8000-000000000001")
    assert event.event_type == "integration.setup"
    assert rendered["payload"] == {
        "view": "validation",
        "template_id": "integration_setup_status",
        "status_id": "observed",
        "actor_role_id": "captain_gateway",
        "integration_status": "missing",
        "required_integration_count": 1,
        "ready_integration_count": 0,
    }
    serialized = event.model_dump_json()
    for forbidden in (
        "CRM_PRIMARY",
        "hubspotApi",
        "credential_id",
        "credential_name",
        "project_id",
    ):
        assert forbidden not in serialized


def test_gateway_acknowledges_exact_integration_setup_projection() -> None:
    submission = IntegrationSetupSubmissionV1.model_validate(
        {
            "schema": "captain.integration-setup-submission.v1",
            "event_id": "80000000-0000-4000-8000-000000000001",
            "job_id": "10000000-0000-4000-8000-000000000001",
            "correlation_id": "20000000-0000-4000-8000-000000000001",
            "subject_version": 1,
            "revision": 1,
            "previous_content_sha256": None,
            "occurred_at": "2026-08-04T12:00:00Z",
            "plan": {
                "schema": "captain.integration-setup-plan.v1",
                "connections": [],
            },
        }
    )
    job = {
        "event_id": "30000000-0000-4000-8000-000000000001",
        "correlation_id": str(submission.correlation_id),
        "required_capability": "integration_setup",
    }
    projection = integration_setup_projection(submission, job)
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
        acknowledged_at=datetime(2026, 8, 4, 12, 1, tzinfo=timezone.utc),
        outcome="mirrored",
    )
    appended = []

    class Store(GatewayStore):
        def __init__(self) -> None:
            pass

        def factory_promotion_source(self, projection_event_id):
            del projection_event_id
            return None

        def integration_setup_source(self, projection_event_id):
            assert projection_event_id == submission.event_id
            return submission, job

        def append_delivery_event(self, event, *, require_current_claim=False):
            assert require_current_claim is False
            appended.append(event)
            return AppendResult(event=event, replayed=False)

    result = Store().record_minibook_projection_acknowledgement(acknowledgement)

    assert result.replayed is False
    assert appended == [
        integration_setup_registry_mirror_event(acknowledgement, submission, job)
    ]
