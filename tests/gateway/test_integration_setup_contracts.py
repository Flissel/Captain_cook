from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import ValidationError

from agenten.agent_factory.integration_setup import IntegrationSetupPlanV1
from gateway.integration_setup_contracts import (
    IntegrationSetupMutationV1,
    IntegrationSetupSubmissionV1,
    PersistedIntegrationSetupV1,
    apply_integration_setup_mutation,
    build_integration_setup_surface,
    validate_integration_setup_transition,
)
from tests.agent_factory.test_integration_setup import (
    credential,
    integration,
    receipt,
    requirement,
)
from agenten.agent_factory.integration_setup import IntegrationSetupPlanner


def payload() -> dict[str, object]:
    return {
        "schema": "captain.integration-setup-submission.v1",
        "event_id": "80000000-0000-0000-0000-000000000001",
        "job_id": "10000000-0000-0000-0000-000000000001",
        "correlation_id": "20000000-0000-0000-0000-000000000001",
        "subject_version": 1,
        "revision": 1,
        "previous_content_sha256": None,
        "occurred_at": "2026-08-04T12:00:00Z",
        "plan": {
            "schema": "captain.integration-setup-plan.v1",
            "connections": [],
        },
    }


def test_setup_submission_is_frozen_typed_and_utc() -> None:
    submission = IntegrationSetupSubmissionV1.model_validate(payload())

    assert submission.event_id == UUID("80000000-0000-0000-0000-000000000001")
    assert isinstance(submission.plan, IntegrationSetupPlanV1)
    assert submission.occurred_at == datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)

    invalid = payload()
    invalid["occurred_at"] = "2026-08-04T12:00:00"
    with pytest.raises(ValidationError, match="UTC"):
        IntegrationSetupSubmissionV1.model_validate(invalid)


def test_first_revision_cannot_claim_a_previous_digest() -> None:
    invalid = payload()
    invalid["previous_content_sha256"] = "0" * 64

    with pytest.raises(ValidationError, match="first integration setup revision"):
        IntegrationSetupSubmissionV1.model_validate(invalid)


def test_setup_surface_links_to_n8n_without_accepting_secrets() -> None:
    submission = IntegrationSetupSubmissionV1.model_validate(payload())
    persisted = PersistedIntegrationSetupV1(
        submission=submission,
        content_sha256="a" * 64,
    )

    surface = build_integration_setup_surface(
        persisted,
        n8n_ui_base_url="http://localhost:5679",
    )

    assert surface.job_id == submission.job_id
    assert surface.overall_status == "ready"
    assert surface.actions == ()
    assert surface.n8n_credentials_url == "http://localhost:5679/home/credentials"

    with pytest.raises(ValueError, match="safe HTTP"):
        build_integration_setup_surface(
            persisted,
            n8n_ui_base_url="http://user:secret@localhost:5679",
        )


def test_rotation_and_revoke_are_explicit_digest_fenced_transitions() -> None:
    current = IntegrationSetupSubmissionV1.model_validate(
        payload()
        | {
            "plan": IntegrationSetupPlanner().plan(
                integrations=(integration(),),
                requirements=(requirement(),),
                credentials=(credential(),),
                verification_receipts=(receipt(),),
            ).model_dump(mode="json", by_alias=True)
        }
    )
    persisted = PersistedIntegrationSetupV1(
        submission=current,
        content_sha256="a" * 64,
    )
    rotation = IntegrationSetupMutationV1.model_validate(
        {
            "schema": "captain.integration-setup-mutation.v1",
            "event_id": "80000000-0000-0000-0000-000000000002",
            "credential_alias": "CRM_API_KEY",
            "expected_content_sha256": "a" * 64,
            "occurred_at": "2026-08-04T13:00:00Z",
            "action": "rotation_requested",
        }
    )

    rotated = apply_integration_setup_mutation(persisted, rotation)

    assert rotated.revision == 2
    assert rotated.previous_content_sha256 == "a" * 64
    assert rotated.change_kind == "rotation_requested"
    assert rotated.plan.connections[0].status == "verification_required"
    assert rotated.plan.connections[0].verification_receipt is None
    assert rotated.plan.connections[0].selected_credential == credential()

    revoked = apply_integration_setup_mutation(
        PersistedIntegrationSetupV1(
            submission=rotated,
            content_sha256="b" * 64,
        ),
        rotation.model_copy(
            update={
                "event_id": UUID("80000000-0000-0000-0000-000000000003"),
                "expected_content_sha256": "b" * 64,
                "action": "revoked",
            }
        ),
    )
    assert revoked.change_kind == "revoked"
    assert revoked.plan.connections[0].status == "revoked"

    with pytest.raises(ValueError, match="digest fence"):
        apply_integration_setup_mutation(
            persisted,
            rotation.model_copy(update={"expected_content_sha256": "c" * 64}),
        )


def test_rotation_requires_new_provider_evidence_before_ready() -> None:
    ready = IntegrationSetupSubmissionV1.model_validate(
        payload()
        | {
            "plan": IntegrationSetupPlanner().plan(
                integrations=(integration(),),
                requirements=(requirement(),),
                credentials=(credential(),),
                verification_receipts=(receipt(),),
            ).model_dump(mode="json", by_alias=True)
        }
    )
    rotated = apply_integration_setup_mutation(
        PersistedIntegrationSetupV1(submission=ready, content_sha256="a" * 64),
        IntegrationSetupMutationV1(
            event_id=UUID("80000000-0000-0000-0000-000000000002"),
            credential_alias="CRM_API_KEY",
            expected_content_sha256="a" * 64,
            occurred_at=datetime(2026, 8, 4, 13, tzinfo=timezone.utc),
            action="rotation_requested",
        ),
    )
    stale_ready = ready.model_copy(
        update={
            "event_id": UUID("80000000-0000-0000-0000-000000000003"),
            "revision": 3,
            "previous_content_sha256": "b" * 64,
            "occurred_at": datetime(2026, 8, 4, 14, tzinfo=timezone.utc),
        }
    )

    with pytest.raises(ValueError, match="fresh provider verification"):
        validate_integration_setup_transition(rotated, stale_ready)
