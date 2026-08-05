#!/usr/bin/env python3
"""Run one real Supabase -> Captain -> n8n -> provider Bearer proof."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import ssl
import tempfile
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit
from uuid import UUID, uuid4

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.contracts import (
    AgentFactoryJob,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.input_contracts import RequestedIntegration
from agenten.agent_factory.integration_setup import (
    IntegrationCredentialRequirementV1,
    IntegrationSetupPlanner,
    N8nCredentialMetadataV1,
)
from agenten.agent_factory.integration_setup_n8n import (
    CaptainN8nCredentialMetadataClient,
)
from agenten.agent_factory.leases import issue_factory_lease
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent
from agenten.agent_runtime.n8n_endpoint import resolve_n8n_endpoint
from gateway.integration_setup_contracts import IntegrationSetupSubmissionV1
from gateway.portal_live_contracts import (
    PortalProviderAuditV1,
    PortalProviderProbeCompletionV1,
)
from gateway.settings import GatewaySettings
from scripts.provision_portal_demo_identity import validate_session


_ROOT_KEYS = {
    "CAPTAIN_GATEWAY_TOKEN",
    "CAPTAIN_GATEWAY_URL",
    "CAPTAIN_N8N_API_KEY",
    "CAPTAIN_N8N_MCP_BROKER_URL",
    "CAPTAIN_N8N_MCP_TOKEN",
    "CAPTAIN_N8N_URL",
    "CAPTAIN_PORTAL_GITEA_ORIGIN",
    "CAPTAIN_PORTAL_N8N_ADAPTERS_ENABLED",
    "CAPTAIN_PORTAL_VERIFICATION_RELEASES_JSON",
    "LEDGER_DSN",
    "N8N_MODE",
    "PORTAL_EVIDENCE_TOKEN",
    "PORTAL_ORGANIZATION_CLAIM",
    "PORTAL_PROVIDER_CONTROL_TOKEN",
    "PORTAL_RESTART_CONTROL_TOKEN",
    "PORTAL_SUPABASE_AUDIENCE",
    "PORTAL_SUPABASE_ISSUER",
    "PORTAL_SUPABASE_JWKS_URL",
    "SSL_CERT_FILE",
    "WORKER_GATEWAY_TOKEN",
}
_IDENTITY_KEYS = {
    "CAPTAIN_PORTAL_DEMO_EMAIL",
    "CAPTAIN_PORTAL_DEMO_PASSWORD",
    "CAPTAIN_PORTAL_DEMO_ORGANIZATION_ID",
    "CAPTAIN_PORTAL_DEMO_SUBJECT_ID",
    "CAPTAIN_PORTAL_SUPABASE_ANON_KEY",
}
_CREDENTIAL_KEYS = {
    "CAPTAIN_N8N_BEARER_CREDENTIAL_NAME",
    "CAPTAIN_N8N_OAUTH2_CREDENTIAL_NAME",
}


def _read_environment(path: Path, allowed: set[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.resolve(strict=True).read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError("live environment contains an invalid line")
        key, value = line.split("=", 1)
        if key not in allowed:
            continue
        if key in values or not value or "\r" in value or "\n" in value:
            raise RuntimeError("live environment contains an invalid value")
        values[key] = value
    return values


def _required(values: Mapping[str, str], name: str) -> str:
    value = values.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing required live setting: {name}")
    return value


def select_credential(
    credentials: Iterable[N8nCredentialMetadataV1],
    expected_name: str,
    expected_type: str = "httpBearerAuth",
) -> N8nCredentialMetadataV1:
    matches = tuple(
        credential
        for credential in credentials
        if credential.credential_name == expected_name
        and credential.credential_type == expected_type
    )
    if len(matches) != 1:
        raise RuntimeError("provider live gate requires exactly one named credential")
    selected = matches[0]
    if selected.project_id is None:
        raise RuntimeError("provider live gate requires a project-bound credential")
    return selected


def build_evidence(
    *,
    job_id: UUID,
    correlation_id: UUID,
    run_id: str,
    credential_id: str,
    completion: PortalProviderProbeCompletionV1,
    audit: PortalProviderAuditV1,
) -> dict[str, object]:
    evidence: dict[str, object] = {
        "schema": "captain.portal-provider-live-evidence.v1",
        "status": "passed",
        "job_id": str(job_id),
        "correlation_id": str(correlation_id),
        "run_id": run_id,
        "credential_id": credential_id,
        "provider_trace_id": str(completion.trace_id),
        "provider_proof_sha256": completion.provider_proof_sha256,
        "provider_probe_id": str(completion.provider_probe_id),
        "template_sha256": completion.template_ref.sha256,
        "workflow_sha256": completion.deployed_workflow_ref.sha256,
        "execution_sha256": completion.execution_ref.sha256,
        "provider_invocation_count": audit.invocation_count,
        "provider_completion_count": audit.completion_count,
        "secrets_emitted": False,
    }
    if completion.integration_kind == "oauth2":
        evidence.update(
            {
                "oauth_grant_type": completion.oauth_grant_type,
                "oauth_exchange_id": str(completion.oauth_exchange_id),
                "oauth_exchange_sha256": completion.oauth_exchange_ref.sha256,
            }
        )
    return evidence


def _request_json(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    expected: tuple[int, ...],
    token: str | None = None,
    api_key: str | None = None,
    payload: Mapping[str, object] | None = None,
    timeout: float = 15.0,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if api_key is not None:
        headers["apikey"] = api_key
    try:
        response = client.request(
            method,
            url,
            headers=headers,
            json=payload,
            timeout=timeout,
            follow_redirects=False,
        )
    except httpx.HTTPError:
        raise RuntimeError("portal Bearer live request failed") from None
    if response.status_code not in expected or len(response.content) > 512 * 1024:
        endpoint = urlsplit(url).path
        raise RuntimeError(
            f"portal Bearer live request failed at {endpoint} "
            f"with HTTP {response.status_code}"
        )
    try:
        value = response.json()
    except ValueError:
        raise RuntimeError("portal Bearer live response was invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("portal Bearer live response was invalid")
    return value


def _ticket(
    client: httpx.Client,
    *,
    gateway_url: str,
    job_id: UUID,
    access_token: str,
    alias: str,
    action: str,
) -> dict[str, Any]:
    return _request_json(
        client,
        "POST",
        f"{gateway_url}/v1/portal/integration-setups/{job_id}/tickets",
        expected=(201,),
        token=access_token,
        payload={"credential_alias": alias, "action": action},
    )


def _ticket_payload(ticket: Mapping[str, Any], alias: str) -> dict[str, object]:
    return {
        "ticket_id": ticket["ticket_id"],
        "ticket": ticket["ticket"],
        "credential_alias": alias,
    }


async def _discover_credentials(
    *,
    values: Mapping[str, str],
    lease: object,
    requirement: IntegrationCredentialRequirementV1,
    now: datetime,
    verify: ssl.SSLContext,
) -> tuple[N8nCredentialMetadataV1, ...]:
    endpoint = resolve_n8n_endpoint(dict(values) | {"N8N_MODE": "captain-builder"})
    async with httpx.AsyncClient(verify=verify, trust_env=False) as http:
        return await CaptainN8nCredentialMetadataClient(http=http).discover(
            lease=lease,
            endpoint=endpoint,
            requirement=requirement,
            now=now,
            timeout_seconds=10.0,
        )


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def run(
    *,
    root_env: Path,
    n8n_credentials_env: Path,
    identity_env: Path,
    evidence_path: Path,
    integration_kind: str = "bearer",
) -> dict[str, object]:
    values = _read_environment(root_env, _ROOT_KEYS)
    values.update(_read_environment(n8n_credentials_env, _CREDENTIAL_KEYS))
    identity = _read_environment(identity_env, _IDENTITY_KEYS)
    settings = GatewaySettings.from_env(values)
    if not settings.portal_control_configured or not settings.portal_n8n_adapters_configured:
        raise RuntimeError("portal provider live controls are not completely configured")
    if integration_kind not in {"bearer", "oauth2"}:
        raise RuntimeError("unsupported provider integration kind")
    is_oauth = integration_kind == "oauth2"
    credential_type = "oAuth2Api" if is_oauth else "httpBearerAuth"
    credential_alias = (
        "CONTROLLED_PROVIDER_OAUTH" if is_oauth else "CONTROLLED_PROVIDER_BEARER"
    )
    credential_name_key = (
        "CAPTAIN_N8N_OAUTH2_CREDENTIAL_NAME"
        if is_oauth
        else "CAPTAIN_N8N_BEARER_CREDENTIAL_NAME"
    )
    release_name = "oauth2.json" if is_oauth else "bearer.json"
    ca_path = _required(values, "SSL_CERT_FILE")
    verify = ssl.create_default_context(cafile=ca_path)
    gateway_url = _required(values, "CAPTAIN_GATEWAY_URL").rstrip("/")
    captain_token = _required(values, "CAPTAIN_GATEWAY_TOKEN")
    provider_token = _required(values, "PORTAL_PROVIDER_CONTROL_TOKEN")
    worker_token = _required(values, "WORKER_GATEWAY_TOKEN")
    issuer = _required(values, "PORTAL_SUPABASE_ISSUER").rstrip("/")
    expected_name = _required(values, credential_name_key)
    releases = tuple(
        release
        for release in settings.portal_verification_releases
        if release.path.endswith(f"/{release_name}")
    )
    if len(releases) != 1:
        raise RuntimeError("provider live gate requires one pinned verification release")
    release = releases[0]

    now = datetime.now(timezone.utc)
    job_id = uuid4()
    correlation_id = uuid4()
    input_digest = hashlib.sha256(
        f"portal-{integration_kind}-live|{job_id}|{correlation_id}".encode("utf-8")
    ).hexdigest()
    job = AgentFactoryJob(
        schema_name="captain.agent-factory-job.v1",
        event_id=uuid4(),
        correlation_id=correlation_id,
        occurred_at=now,
        producer="captain",
        job_id=job_id,
        subject_version=1,
        input_ref=ArtifactRef(
            uri=f"artifact://portal-{integration_kind}-live/{input_digest}",
            sha256=input_digest,
            media_type="application/json",
        ),
        required_capability=f"portal_{integration_kind}_verification",
        acceptance_assertion_ids=("provider_trace_bound", "provider_proof_bound"),
        max_behavioral_iterations=5,
    )
    architect_lease = issue_factory_lease(
        job=job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref=f"workspace://captain/portal-{integration_kind}-live/architecture",
        now=now,
    )
    lease = issue_factory_lease(
        job=job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref=f"workspace://captain/portal-{integration_kind}-live",
        now=now,
        integration_intent=IntegrationIntent.N8N,
    )
    forge = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid4(),
        job_id=job_id,
        correlation_id=correlation_id,
        causation_id=job.event_id,
        occurred_at=now,
        producer="captain",
        subject_version=1,
        attempt=1,
        phase=FactoryPhase.FORGE_REQUESTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(job.input_ref,),
        evidence_refs=(job.input_ref,),
    )
    blueprint_digest = hashlib.sha256(
        f"portal-{integration_kind}-blueprint|{job_id}|{release.sha256}".encode("utf-8")
    ).hexdigest()
    blueprint_ref = ArtifactRef(
        uri=f"artifact://portal-{integration_kind}-blueprint/{blueprint_digest}",
        sha256=blueprint_digest,
        media_type="application/json",
    )
    blueprint = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid4(),
        job_id=job_id,
        correlation_id=correlation_id,
        causation_id=forge.event_id,
        occurred_at=now,
        producer="hermes",
        subject_version=1,
        attempt=1,
        phase=FactoryPhase.BLUEPRINT_CREATED,
        role=FactoryRole.AGENT_ARCHITECT,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(blueprint_ref,),
        evidence_refs=(blueprint_ref,),
        lease_id=architect_lease.lease_id,
    )
    discovery_requirement = IntegrationCredentialRequirementV1(
        integration_key="controlled_provider",
        credential_alias=credential_alias,
        credential_type=credential_type,
        required=True,
        setup_method="n8n_ui",
        setup_label=f"Controlled Provider {integration_kind}",
        verification_workflow_sha256=release.sha256,
    )
    discovered = asyncio.run(
        _discover_credentials(
            values=values,
            lease=lease,
            requirement=discovery_requirement,
            now=now,
            verify=verify,
        )
    )
    selected = select_credential(discovered, expected_name, credential_type)
    requirement = discovery_requirement.model_copy(
        update={"project_id": selected.project_id}
    )
    integration = RequestedIntegration(
        integration_key="controlled_provider",
        purpose=f"Verify one harmless provider-backed {integration_kind} request",
        trigger="Captain runs the bounded portal verification probe",
        operation="POST one correlation-bound provider probe",
        required=True,
        credential_aliases=(requirement.credential_alias,),
        success_behavior="Persist provider trace and proof references",
        failure_behavior="Fail closed without a readiness claim",
    )
    plan = IntegrationSetupPlanner().plan(
        integrations=(integration,),
        requirements=(requirement,),
        credentials=(),
    )
    setup = IntegrationSetupSubmissionV1(
        event_id=uuid4(),
        job_id=job_id,
        correlation_id=correlation_id,
        subject_version=1,
        revision=1,
        occurred_at=now,
        plan=plan,
    )

    with httpx.Client(verify=verify, trust_env=False) as client:
        _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/factory/jobs",
            expected=(202,),
            token=captain_token,
            payload=job.model_dump(mode="json", by_alias=True),
        )
        _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/factory/blocks",
            expected=(201,),
            token=captain_token,
            payload=forge.model_dump(mode="json", by_alias=True),
        )
        _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/factory/leases",
            expected=(201,),
            token=captain_token,
            payload=architect_lease.model_dump(mode="json", by_alias=True),
        )
        _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/factory/blocks",
            expected=(201,),
            token=worker_token,
            payload=blueprint.model_dump(mode="json", by_alias=True),
        )
        _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/factory/leases",
            expected=(201,),
            token=captain_token,
            payload=lease.model_dump(mode="json", by_alias=True),
        )
        _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/factory/integration-setups",
            expected=(201,),
            token=captain_token,
            payload=setup.model_dump(mode="json", by_alias=True),
        )
        _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/factory/integration-setups/{job_id}/portal-tenant-binding",
            expected=(201,),
            token=captain_token,
            payload={
                "job_id": str(job_id),
                "organization_id": _required(
                    identity, "CAPTAIN_PORTAL_DEMO_ORGANIZATION_ID"
                ),
            },
        )
        session = _request_json(
            client,
            "POST",
            f"{issuer}/token?grant_type=password",
            expected=(200,),
            api_key=_required(identity, "CAPTAIN_PORTAL_SUPABASE_ANON_KEY"),
            payload={
                "email": _required(identity, "CAPTAIN_PORTAL_DEMO_EMAIL"),
                "password": _required(identity, "CAPTAIN_PORTAL_DEMO_PASSWORD"),
            },
        )
        access_token = session.get("access_token")
        if not isinstance(access_token, str):
            raise RuntimeError("Supabase login did not return a portal session")
        validate_session(
            access_token,
            organization_id=_required(identity, "CAPTAIN_PORTAL_DEMO_ORGANIZATION_ID"),
        )
        discover_ticket = _ticket(
            client,
            gateway_url=gateway_url,
            job_id=job_id,
            access_token=access_token,
            alias=requirement.credential_alias,
            action="discover",
        )
        discovered_surface = _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/portal/integration-setups/{job_id}/discover",
            expected=(200,),
            token=access_token,
            payload=_ticket_payload(discover_ticket, requirement.credential_alias),
        )
        actions = discovered_surface.get("actions")
        if not isinstance(actions, list):
            raise RuntimeError("portal discovery response was invalid")
        candidates = tuple(
            candidate
            for action in actions
            if isinstance(action, dict)
            and action.get("credential_alias") == requirement.credential_alias
            for candidate in action.get("candidate_credentials", [])
            if isinstance(candidate, dict)
            and candidate.get("credential_id") == selected.credential_id
        )
        if len(candidates) != 1:
            raise RuntimeError("portal discovery did not return the selected credential")
        select_ticket = _ticket(
            client,
            gateway_url=gateway_url,
            job_id=job_id,
            access_token=access_token,
            alias=requirement.credential_alias,
            action="select",
        )
        selected_surface = _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/portal/integration-setups/{job_id}/select",
            expected=(200,),
            token=access_token,
            payload={
                **_ticket_payload(select_ticket, requirement.credential_alias),
                "credential_id": selected.credential_id,
            },
        )
        verify_ticket = _ticket(
            client,
            gateway_url=gateway_url,
            job_id=job_id,
            access_token=access_token,
            alias=requirement.credential_alias,
            action="verify",
        )
        verified_surface = _request_json(
            client,
            "POST",
            f"{gateway_url}/v1/portal/integration-setups/{job_id}/verify",
            expected=(200,),
            token=access_token,
            payload=_ticket_payload(verify_ticket, requirement.credential_alias),
            timeout=60.0,
        )
        verified_actions = verified_surface.get("actions")
        if not isinstance(verified_actions, list):
            raise RuntimeError("portal verification response was invalid")
        verified_target = tuple(
            action
            for action in verified_actions
            if isinstance(action, dict)
            and action.get("credential_alias") == requirement.credential_alias
        )
        if len(verified_target) != 1 or verified_target[0].get("status") != "ready":
            raise RuntimeError("portal verification did not produce a ready connection")
        revision = verified_surface.get("revision")
        content_sha256 = verified_surface.get("content_sha256")
        if not isinstance(revision, int) or not isinstance(content_sha256, str):
            raise RuntimeError("portal selection response was invalid")
        run_id = f"portal-{integration_kind}-{uuid4().hex[:16]}"
        probe_request_id = uuid4()
        completion = PortalProviderProbeCompletionV1.model_validate(
            _request_json(
                client,
                "POST",
                f"{gateway_url}/v1/control/provider/probes",
                expected=(200,),
                token=provider_token,
                payload={
                    "probe_request_id": str(probe_request_id),
                    "run_id": run_id,
                    "job_id": str(job_id),
                    "correlation_id": str(correlation_id),
                    "integration_kind": integration_kind,
                    "credential_alias": requirement.credential_alias,
                    "credential_id": selected.credential_id,
                    "setup_revision": revision,
                    "setup_content_sha256": content_sha256,
                    "verification_template_sha256": release.sha256,
                },
                timeout=60.0,
            )
        )
        audit = PortalProviderAuditV1.model_validate(
            _request_json(
                client,
                "POST",
                f"{gateway_url}/v1/control/provider/audit",
                expected=(200,),
                token=provider_token,
                payload={
                    "run_id": run_id,
                    "job_id": str(job_id),
                    "correlation_id": str(correlation_id),
                },
            )
        )
    if (
        completion.trace_id not in audit.trace_ids
        or audit.invocation_count != 1
        or audit.completion_count != 1
    ):
        raise RuntimeError("Gateway provider audit did not match the live completion")
    evidence = build_evidence(
        job_id=job_id,
        correlation_id=correlation_id,
        run_id=run_id,
        credential_id=selected.credential_id,
        completion=completion,
        audit=audit,
    )
    _atomic_json(evidence_path, evidence)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-env", type=Path, default=Path(".env"))
    parser.add_argument(
        "--n8n-credentials-env",
        type=Path,
        default=Path(".env.n8n-credentials"),
    )
    parser.add_argument("--identity-env", type=Path, required=True)
    parser.add_argument(
        "--integration-kind",
        choices=("bearer", "oauth2"),
        default="bearer",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=None,
    )
    args = parser.parse_args()
    evidence = run(
        root_env=args.root_env,
        n8n_credentials_env=args.n8n_credentials_env,
        identity_env=args.identity_env,
        evidence_path=(
            args.evidence
            or Path(f".captain-cook/evidence/portal-{args.integration_kind}-live.json")
        ),
        integration_kind=args.integration_kind,
    )
    print(
        json.dumps(
            {
                "status": evidence["status"],
                "schema": evidence["schema"],
                "secrets_emitted": False,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
