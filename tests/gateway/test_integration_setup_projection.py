from __future__ import annotations

from uuid import UUID

from gateway.integration_setup_contracts import IntegrationSetupSubmissionV1
from gateway.registry_feed import integration_setup_projection


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
