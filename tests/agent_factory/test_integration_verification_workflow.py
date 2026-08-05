from __future__ import annotations

import hashlib
import json

import pytest

from agenten.agent_factory.gitea_templates import VerifiedTemplatePayload
from agenten.agent_factory.integration_setup import (
    IntegrationCredentialRequirementV1,
    N8nCredentialMetadataV1,
)
from agenten.agent_factory.integration_verification_workflow import (
    materialize_verification_workflow,
)
from agenten.agent_runtime.contracts import ArtifactRef


def _requirement(**changes: object) -> IntegrationCredentialRequirementV1:
    values: dict[str, object] = {
        "integration_key": "crm",
        "credential_alias": "CRM_API_KEY",
        "credential_type": "httpBearerAuth",
        "required": True,
        "setup_method": "n8n_ui",
        "setup_label": "Bearer Auth",
        "project_id": "captain-production",
        "verification_workflow_sha256": "a" * 64,
    }
    values.update(changes)
    return IntegrationCredentialRequirementV1(**values)


def _credential() -> N8nCredentialMetadataV1:
    return N8nCredentialMetadataV1(
        credential_id="cred-prod",
        credential_name="CRM production",
        credential_type="httpBearerAuth",
        project_id="captain-production",
    )


def _template(workflow: dict[str, object]) -> VerifiedTemplatePayload:
    content = json.dumps(workflow, separators=(",", ":"), sort_keys=True).encode()
    digest = hashlib.sha256(content).hexdigest()
    return VerifiedTemplatePayload(
        ref=ArtifactRef(
            uri=f"artifact://gitea/{digest}",
            sha256=digest,
            media_type="application/json",
        ),
        content=content,
    )


def _workflow() -> dict[str, object]:
    return {
        "nodes": [
            {
                "name": "Provider probe",
                "type": "n8n-nodes-base.httpRequest",
                "parameters": {"url": "https://provider.example/health"},
                "credentials": {
                    "httpBearerAuth": {
                        "id": "{{CAPTAIN_CREDENTIAL_ID}}",
                        "name": "{{CAPTAIN_CREDENTIAL_NAME}}",
                    }
                },
            }
        ],
        "connections": {},
    }


def test_materializer_binds_exact_credential_and_seals_new_digest() -> None:
    template = _template(_workflow())
    requirement = _requirement(verification_workflow_sha256=template.ref.sha256)

    result = materialize_verification_workflow(
        template=template,
        requirement=requirement,
        credential=_credential(),
    )

    bound = result.artifact.workflow["nodes"][0]["credentials"]["httpBearerAuth"]
    assert bound == {"id": "cred-prod", "name": "CRM production"}
    assert result.template_ref == template.ref
    assert result.artifact.artifact_digest != template.ref.sha256
    assert "{{CAPTAIN_CREDENTIAL" not in result.artifact.model_dump_json()


@pytest.mark.parametrize(
    "workflow",
    [
        {"nodes": [], "connections": {}},
        {
            "nodes": [
                _workflow()["nodes"][0],
                _workflow()["nodes"][0],
            ],
            "connections": {},
        },
        {
            "nodes": [
                {
                    **_workflow()["nodes"][0],
                    "credentials": {
                        "oAuth2Api": {
                            "id": "{{CAPTAIN_CREDENTIAL_ID}}",
                            "name": "{{CAPTAIN_CREDENTIAL_NAME}}",
                        }
                    },
                }
            ],
            "connections": {},
        },
    ],
)
def test_materializer_rejects_missing_multiple_or_wrong_type_placeholders(
    workflow: dict[str, object],
) -> None:
    template = _template(workflow)
    requirement = _requirement(verification_workflow_sha256=template.ref.sha256)

    with pytest.raises(ValueError, match="credential placeholder"):
        materialize_verification_workflow(
            template=template,
            requirement=requirement,
            credential=_credential(),
        )
