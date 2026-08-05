"""Materialize a released verification template without credential values."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from agenten.agent_factory.gitea_templates import VerifiedTemplatePayload
from agenten.agent_factory.integration_setup import (
    IntegrationCredentialRequirementV1,
    N8nCredentialMetadataV1,
)
from agenten.agent_runtime.contracts import ArtifactRef
from agenten.targets.n8n import SealedArtifact


_CREDENTIAL_ID_PLACEHOLDER = "{{CAPTAIN_CREDENTIAL_ID}}"
_CREDENTIAL_NAME_PLACEHOLDER = "{{CAPTAIN_CREDENTIAL_NAME}}"


@dataclass(frozen=True)
class BoundVerificationWorkflow:
    template_ref: ArtifactRef
    artifact: SealedArtifact


def _canonical_bytes(value: dict[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def materialize_verification_workflow(
    *,
    template: VerifiedTemplatePayload,
    requirement: IntegrationCredentialRequirementV1,
    credential: N8nCredentialMetadataV1,
) -> BoundVerificationWorkflow:
    """Bind exactly one released n8n credential placeholder by ID and name."""

    if template.ref.sha256 != requirement.verification_workflow_sha256:
        raise ValueError("verification template does not match Captain release")
    if (
        credential.credential_type != requirement.credential_type
        or credential.project_id != requirement.project_id
    ):
        raise ValueError("credential metadata does not match setup requirement")
    try:
        workflow = json.loads(template.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("verification template is not valid UTF-8 JSON") from None
    if not isinstance(workflow, dict):
        raise ValueError("verification template must be a JSON object")

    matches: list[dict[str, Any]] = []
    nodes = workflow.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            credentials = node.get("credentials")
            if not isinstance(credentials, dict):
                continue
            binding = credentials.get(requirement.credential_type)
            if (
                isinstance(binding, dict)
                and binding.get("id") == _CREDENTIAL_ID_PLACEHOLDER
                and binding.get("name") == _CREDENTIAL_NAME_PLACEHOLDER
            ):
                matches.append(binding)
    if len(matches) != 1:
        raise ValueError("verification workflow must contain exactly one credential placeholder")

    placeholder_count = _canonical_bytes(workflow).count(b"{{CAPTAIN_CREDENTIAL_")
    if placeholder_count != 2:
        raise ValueError("verification workflow contains an invalid credential placeholder")
    matches[0]["id"] = credential.credential_id
    matches[0]["name"] = credential.credential_name

    bound_bytes = _canonical_bytes(workflow)
    bound_digest = hashlib.sha256(bound_bytes).hexdigest()
    artifact = SealedArtifact(
        artifact_id=f"probe-{template.ref.sha256[:12]}",
        artifact_digest=bound_digest,
        namespace="integration-verification",
        workflow=workflow,
    )
    return BoundVerificationWorkflow(template_ref=template.ref, artifact=artifact)
