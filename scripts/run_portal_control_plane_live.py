#!/usr/bin/env python3
"""Run the immutable Bearer/OAuth/restart/Minibook portal release gate."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
from datetime import datetime, timezone
from typing import Mapping
from uuid import uuid4

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from agenten.agent_factory.contracts import (  # noqa: E402
    AgentFactoryJob,
    FactoryBlockStatus,
    FactoryEvidenceBlock,
    FactoryPhase,
    FactoryRole,
)
from agenten.agent_factory.input_contracts import RequestedIntegration  # noqa: E402
from agenten.agent_factory.integration_setup import (  # noqa: E402
    IntegrationCredentialRequirementV1,
    IntegrationSetupPlanner,
    N8nCredentialMetadataV1,
)
from agenten.agent_factory.leases import issue_factory_lease  # noqa: E402
from agenten.agent_runtime.contracts import ArtifactRef, IntegrationIntent  # noqa: E402
from agenten.delivery.minibook_client import MinibookClient  # noqa: E402
from agenten.delivery.minibook_events import (  # noqa: E402
    MinibookProjectionRebuildReceiptV1,
)
from agenten.delivery.projection_cursor import ProjectionCursorStore  # noqa: E402
from agenten.delivery.projector import MinibookProjector  # noqa: E402
from gateway.integration_setup_contracts import (  # noqa: E402
    IntegrationSetupSubmissionV1,
    PersistedIntegrationSetupV1,
)
from gateway.portal_live_contracts import (  # noqa: E402
    PortalLiveEvidenceQueryV1,
    PortalLiveEvidenceV1,
    PortalLiveRunFinalizationV1,
    PortalProviderAuditV1,
    PortalProviderProbeCompletionV1,
    PortalRestartReceiptV1,
)
from gateway.registry_feed import integration_setup_projection  # noqa: E402
from gateway.settings import GatewaySettings  # noqa: E402
from scripts.rebuild_minibook_projection import CaptainProjectionFeed  # noqa: E402
from scripts.run_portal_bearer_live import (  # noqa: E402
    _CREDENTIAL_KEYS,
    _IDENTITY_KEYS,
    _ROOT_KEYS,
    _atomic_json,
    _discover_credentials,
    _read_environment,
    _request_json,
    _required,
    _ticket,
    _ticket_payload,
    select_credential,
)
from scripts.provision_portal_demo_identity import validate_session  # noqa: E402


def provider_sequence() -> tuple[str, str, str]:
    return ("bearer", "oauth2", "bearer")


def terminal_action_sequence() -> tuple[str, str]:
    return ("rotation_requested", "revoked")


def cursor_path_for_run(directory: Path, run_id: str) -> Path:
    if not run_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._" for character in run_id):
        raise ValueError("run identity is unsafe for a cursor path")
    return directory / f"minibook-{run_id}.db"


def aggregate_requirements(
    releases: Mapping[str, str],
) -> tuple[IntegrationCredentialRequirementV1, ...]:
    return (
        IntegrationCredentialRequirementV1(
            integration_key="controlled_provider_bearer",
            credential_alias="CONTROLLED_PROVIDER_BEARER",
            credential_type="httpBearerAuth",
            required=True,
            setup_method="n8n_ui",
            setup_label="Controlled Provider Bearer",
            verification_workflow_sha256=releases["bearer"],
        ),
        IntegrationCredentialRequirementV1(
            integration_key="controlled_provider_oauth2",
            credential_alias="CONTROLLED_PROVIDER_OAUTH",
            credential_type="oAuth2Api",
            required=True,
            setup_method="n8n_ui",
            setup_label="Controlled Provider OAuth2",
            verification_workflow_sha256=releases["oauth2"],
        ),
    )


def _canonical_digests(events: list[dict[str, object]]) -> tuple[str, str]:
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":")).encode()
    identifiers = json.dumps(
        [event["event_id"] for event in events], separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest(), hashlib.sha256(identifiers).hexdigest()


def _release_map(settings: GatewaySettings) -> dict[str, object]:
    result: dict[str, object] = {}
    for kind, filename in (("bearer", "bearer.json"), ("oauth2", "oauth2.json")):
        matches = tuple(
            release
            for release in settings.portal_verification_releases
            if release.path.endswith(f"/{filename}")
        )
        if len(matches) != 1:
            raise RuntimeError("aggregate live gate requires both pinned releases")
        result[kind] = matches[0]
    return result


def _portal_connect(
    client: httpx.Client,
    *,
    gateway_url: str,
    job_id: object,
    access_token: str,
    requirement: IntegrationCredentialRequirementV1,
    credential: N8nCredentialMetadataV1,
) -> dict[str, object]:
    discover = _ticket(
        client,
        gateway_url=gateway_url,
        job_id=job_id,
        access_token=access_token,
        alias=requirement.credential_alias,
        action="discover",
    )
    _request_json(
        client,
        "POST",
        f"{gateway_url}/v1/portal/integration-setups/{job_id}/discover",
        expected=(200,),
        token=access_token,
        payload=_ticket_payload(discover, requirement.credential_alias),
    )
    select = _ticket(
        client,
        gateway_url=gateway_url,
        job_id=job_id,
        access_token=access_token,
        alias=requirement.credential_alias,
        action="select",
    )
    _request_json(
        client,
        "POST",
        f"{gateway_url}/v1/portal/integration-setups/{job_id}/select",
        expected=(200,),
        token=access_token,
        payload={
            **_ticket_payload(select, requirement.credential_alias),
            "credential_id": credential.credential_id,
        },
    )
    verify = _ticket(
        client,
        gateway_url=gateway_url,
        job_id=job_id,
        access_token=access_token,
        alias=requirement.credential_alias,
        action="verify",
    )
    surface = _request_json(
        client,
        "POST",
        f"{gateway_url}/v1/portal/integration-setups/{job_id}/verify",
        expected=(200,),
        token=access_token,
        payload=_ticket_payload(verify, requirement.credential_alias),
        timeout=60.0,
    )
    matching = tuple(
        action
        for action in surface.get("actions", [])
        if isinstance(action, dict)
        and action.get("credential_alias") == requirement.credential_alias
    )
    if len(matching) != 1 or matching[0].get("status") != "ready":
        raise RuntimeError("aggregate portal verification did not become ready")
    return surface


def run(
    *,
    root_env: Path,
    n8n_credentials_env: Path,
    identity_env: Path,
    evidence_path: Path,
) -> dict[str, object]:
    values = _read_environment(root_env, _ROOT_KEYS | {
        "CAPTAIN_DEMO_MINIBOOK_API_KEY",
        "CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY",
    })
    values.update(_read_environment(n8n_credentials_env, _CREDENTIAL_KEYS))
    identity = _read_environment(identity_env, _IDENTITY_KEYS)
    settings = GatewaySettings.from_env(values)
    if not settings.portal_control_configured or not settings.portal_n8n_adapters_configured:
        raise RuntimeError("aggregate portal controls are not completely configured")
    release_objects = _release_map(settings)
    release_digests = {
        kind: release.sha256 for kind, release in release_objects.items()
    }
    requirements = aggregate_requirements(release_digests)
    ca_path = _required(values, "SSL_CERT_FILE")
    verify_tls = ssl.create_default_context(cafile=ca_path)
    gateway_url = _required(values, "CAPTAIN_GATEWAY_URL").rstrip("/")
    captain_token = _required(values, "CAPTAIN_GATEWAY_TOKEN")
    worker_token = _required(values, "WORKER_GATEWAY_TOKEN")
    provider_token = _required(values, "PORTAL_PROVIDER_CONTROL_TOKEN")
    restart_token = _required(values, "PORTAL_RESTART_CONTROL_TOKEN")
    evidence_token = _required(values, "PORTAL_EVIDENCE_TOKEN")
    issuer = _required(values, "PORTAL_SUPABASE_ISSUER").rstrip("/")
    now = datetime.now(timezone.utc)
    job_id, correlation_id = uuid4(), uuid4()
    input_digest = hashlib.sha256(f"portal-aggregate|{job_id}|{correlation_id}".encode()).hexdigest()
    input_ref = ArtifactRef(
        uri=f"artifact://portal-aggregate/{input_digest}",
        sha256=input_digest,
        media_type="application/json",
    )
    job = AgentFactoryJob(
        schema_name="captain.agent-factory-job.v1",
        event_id=uuid4(),
        correlation_id=correlation_id,
        occurred_at=now,
        producer="captain",
        job_id=job_id,
        subject_version=1,
        input_ref=input_ref,
        required_capability="portal_aggregate_verification",
        acceptance_assertion_ids=("three_provider_traces", "restart_resume", "minibook_rebuild"),
        max_behavioral_iterations=5,
    )
    architect_lease = issue_factory_lease(
        job=job,
        role=FactoryRole.AGENT_ARCHITECT,
        attempt=1,
        workspace_ref="workspace://captain/portal-aggregate/architecture",
        now=now,
    )
    tool_lease = issue_factory_lease(
        job=job,
        role=FactoryRole.TOOL_INTEGRATOR,
        attempt=1,
        workspace_ref="workspace://captain/portal-aggregate/integrations",
        now=now,
        integration_intent=IntegrationIntent.N8N,
    )
    forge = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid4(), job_id=job_id, correlation_id=correlation_id,
        causation_id=job.event_id, occurred_at=now, producer="captain",
        subject_version=1, attempt=1, phase=FactoryPhase.FORGE_REQUESTED,
        status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(input_ref,), evidence_refs=(input_ref,),
    )
    blueprint_digest = hashlib.sha256(
        f"portal-aggregate-blueprint|{job_id}|{release_digests}".encode()
    ).hexdigest()
    blueprint_ref = ArtifactRef(
        uri=f"artifact://portal-aggregate-blueprint/{blueprint_digest}",
        sha256=blueprint_digest,
        media_type="application/json",
    )
    blueprint = FactoryEvidenceBlock(
        schema_name="captain.agent-factory-block.v1",
        event_id=uuid4(), job_id=job_id, correlation_id=correlation_id,
        causation_id=forge.event_id, occurred_at=now, producer="hermes",
        subject_version=1, attempt=1, phase=FactoryPhase.BLUEPRINT_CREATED,
        role=FactoryRole.AGENT_ARCHITECT, status=FactoryBlockStatus.SUCCEEDED,
        artifact_refs=(blueprint_ref,), evidence_refs=(blueprint_ref,),
        lease_id=architect_lease.lease_id,
    )
    selected: dict[str, N8nCredentialMetadataV1] = {}
    bound_requirements: list[IntegrationCredentialRequirementV1] = []
    expected_names = {
        "bearer": _required(values, "CAPTAIN_N8N_BEARER_CREDENTIAL_NAME"),
        "oauth2": _required(values, "CAPTAIN_N8N_OAUTH2_CREDENTIAL_NAME"),
    }
    for kind, requirement in zip(("bearer", "oauth2"), requirements):
        discovered = asyncio.run(
            _discover_credentials(
                values=values,
                lease=tool_lease,
                requirement=requirement,
                now=now,
                verify=verify_tls,
            )
        )
        credential = select_credential(
            discovered, expected_names[kind], requirement.credential_type
        )
        selected[kind] = credential
        bound_requirements.append(
            requirement.model_copy(update={"project_id": credential.project_id})
        )
    integrations = tuple(
        RequestedIntegration(
            integration_key=requirement.integration_key,
            purpose=f"Verify provider-backed {kind} authentication",
            trigger="Captain aggregate release gate",
            operation="POST one bounded provider probe",
            required=True,
            credential_aliases=(requirement.credential_alias,),
            success_behavior="Persist immutable provider trace",
            failure_behavior="Fail closed without release",
        )
        for kind, requirement in zip(("bearer", "oauth2"), bound_requirements)
    )
    setup = IntegrationSetupSubmissionV1(
        event_id=uuid4(), job_id=job_id, correlation_id=correlation_id,
        subject_version=1, revision=1, occurred_at=now,
        plan=IntegrationSetupPlanner().plan(
            integrations=integrations,
            requirements=tuple(bound_requirements),
            credentials=(),
        ),
    )
    with httpx.Client(verify=verify_tls, trust_env=False) as client:
        for path, token, document, expected in (
            ("/v1/factory/jobs", captain_token, job, (202,)),
            ("/v1/factory/blocks", captain_token, forge, (201,)),
            ("/v1/factory/leases", captain_token, architect_lease, (201,)),
            ("/v1/factory/blocks", worker_token, blueprint, (201,)),
            ("/v1/factory/leases", captain_token, tool_lease, (201,)),
            ("/v1/factory/integration-setups", captain_token, setup, (201,)),
        ):
            _request_json(
                client, "POST", gateway_url + path, expected=expected, token=token,
                payload=document.model_dump(mode="json", by_alias=True),
            )
        organization_id = _required(identity, "CAPTAIN_PORTAL_DEMO_ORGANIZATION_ID")
        _request_json(
            client, "POST",
            f"{gateway_url}/v1/factory/integration-setups/{job_id}/portal-tenant-binding",
            expected=(201,), token=captain_token,
            payload={"job_id": str(job_id), "organization_id": organization_id},
        )
        session = _request_json(
            client, "POST", f"{issuer}/token?grant_type=password", expected=(200,),
            api_key=_required(identity, "CAPTAIN_PORTAL_SUPABASE_ANON_KEY"),
            payload={
                "email": _required(identity, "CAPTAIN_PORTAL_DEMO_EMAIL"),
                "password": _required(identity, "CAPTAIN_PORTAL_DEMO_PASSWORD"),
            },
        )
        access_token = session.get("access_token")
        if not isinstance(access_token, str):
            raise RuntimeError("Supabase login did not return a portal session")
        validate_session(access_token, organization_id=organization_id)
        surface: dict[str, object] = {}
        for kind, requirement in zip(("bearer", "oauth2"), bound_requirements):
            surface = _portal_connect(
                client, gateway_url=gateway_url, job_id=job_id,
                access_token=access_token, requirement=requirement,
                credential=selected[kind],
            )
        revision, content_sha256 = surface.get("revision"), surface.get("content_sha256")
        if not isinstance(revision, int) or not isinstance(content_sha256, str):
            raise RuntimeError("aggregate setup surface is invalid")
        run_id = f"portal-aggregate-{uuid4().hex[:16]}"
        completions: list[PortalProviderProbeCompletionV1] = []
        requirement_by_kind = dict(zip(("bearer", "oauth2"), bound_requirements))
        for kind in provider_sequence():
            requirement = requirement_by_kind[kind]
            completion = PortalProviderProbeCompletionV1.model_validate(
                _request_json(
                    client, "POST", f"{gateway_url}/v1/control/provider/probes",
                    expected=(200,), token=provider_token,
                    payload={
                        "probe_request_id": str(uuid4()), "run_id": run_id,
                        "job_id": str(job_id), "correlation_id": str(correlation_id),
                        "integration_kind": kind,
                        "credential_alias": requirement.credential_alias,
                        "credential_id": selected[kind].credential_id,
                        "setup_revision": revision,
                        "setup_content_sha256": content_sha256,
                        "verification_template_sha256": release_digests[kind],
                    }, timeout=60.0,
                )
            )
            completions.append(completion)
        audit = PortalProviderAuditV1.model_validate(
            _request_json(
                client, "POST", f"{gateway_url}/v1/control/provider/audit",
                expected=(200,), token=provider_token,
                payload={"run_id": run_id, "job_id": str(job_id), "correlation_id": str(correlation_id)},
            )
        )
        if audit.invocation_count != 3 or audit.completion_count != 3:
            raise RuntimeError("aggregate provider audit is incomplete")
        previous_boot = _request_json(
            client, "GET", f"{gateway_url}/v1/control/restarts/health",
            expected=(200,), token=restart_token,
        )["boot_id"]

    restart_process = subprocess.run(
        ["pwsh", "-NoProfile", "-File", "scripts/live-demo-services.ps1", "gateway-restart"],
        cwd=REPOSITORY_ROOT, capture_output=True, text=True, timeout=120, check=False,
    )
    if restart_process.returncode != 0:
        raise RuntimeError("controlled Gateway restart failed")
    with httpx.Client(verify=verify_tls, trust_env=False) as client:
        new_boot = _request_json(
            client, "GET", f"{gateway_url}/v1/control/restarts/health",
            expected=(200,), token=restart_token,
        )["boot_id"]
        persisted = PersistedIntegrationSetupV1.model_validate(
            _request_json(
                client, "GET", f"{gateway_url}/v1/factory/jobs/{job_id}/integration-setup",
                expected=(200,), token=captain_token,
            )
        )
        restart = PortalRestartReceiptV1(
            restart_request_id=uuid4(), restart_id=uuid4(), run_id=run_id,
            job_id=job_id, correlation_id=correlation_id,
            services=("gateway", "portal"),
            previous_gateway_boot_id=str(previous_boot), new_gateway_boot_id=str(new_boot),
            portal_deployment_id=f"captain-local-{REPOSITORY_ROOT.name}",
            setup_revision=persisted.submission.revision,
            setup_content_sha256=persisted.content_sha256,
            status="resumed", occurred_at=datetime.now(timezone.utc),
        )
        _request_json(
            client, "POST",
            f"{gateway_url}/v1/control/restarts/{restart.restart_id}/receipts",
            expected=(200,), token=restart_token,
            payload=restart.model_dump(mode="json", by_alias=True),
        )

    child_env = os.environ.copy()
    child_env.update(
        {
            "CAPTAIN_GATEWAY_TOKEN": captain_token,
            "MINIBOOK_API_KEY": _required(values, "CAPTAIN_DEMO_MINIBOOK_API_KEY"),
            "MINIBOOK_PROJECTION_API_KEY": _required(
                values, "CAPTAIN_DEMO_MINIBOOK_PROJECTION_API_KEY"
            ),
        }
    )
    cursor_db = cursor_path_for_run(
        REPOSITORY_ROOT / ".captain-cook/private/portal-live",
        run_id,
    )
    rebuilt = subprocess.run(
        [
            sys.executable, "scripts/rebuild_minibook_projection.py",
            "--captain-url", gateway_url, "--minibook-url", "http://127.0.0.1:8080",
            "--cursor-db", str(cursor_db), "--apply", "--full-rebuild",
        ],
        cwd=REPOSITORY_ROOT, env=child_env, capture_output=True, text=True,
        timeout=180, check=False,
    )
    if rebuilt.returncode != 0:
        raise RuntimeError("Minibook full rebuild failed")
    rebuild_output = json.loads(rebuilt.stdout)
    if rebuild_output.get("mode") != "full-rebuild":
        raise RuntimeError("Minibook full rebuild evidence is invalid")
    feed = CaptainProjectionFeed(gateway_url, token=captain_token)
    events = list(feed.iter_events(cursor=None))
    feed.close()
    event_documents = [event.model_dump(mode="json", by_alias=True) for event in events]
    feed_sha256, event_ids_sha256 = _canonical_digests(event_documents)
    projection = integration_setup_projection(
        persisted.submission, job.model_dump(mode="json", by_alias=True)
    )
    minibook = MinibookClient(
        "http://127.0.0.1:8080",
        child_env["MINIBOOK_API_KEY"],
        projection_api_key=child_env["MINIBOOK_PROJECTION_API_KEY"],
    )
    try:
        acknowledgement = MinibookProjector(
            minibook, ProjectionCursorStore(cursor_db)
        ).acknowledgement(projection)
    finally:
        minibook.close()
    rebuild_receipt = MinibookProjectionRebuildReceiptV1(
        rebuild_id=uuid4(), run_id=run_id, job_id=job_id,
        correlation_id=correlation_id, projection_event_id=projection.event_id,
        acknowledgement_id=acknowledgement.acknowledgement_id,
        setup_revision=persisted.submission.revision,
        setup_content_sha256=persisted.content_sha256,
        feed_sha256=feed_sha256, event_ids_sha256=event_ids_sha256,
        target_project_id=MinibookProjector.PROJECTION_PROJECT_ID,
        outcome="converged", occurred_at=datetime.now(timezone.utc),
    )
    with httpx.Client(verify=verify_tls, trust_env=False) as client:
        _request_json(
            client, "POST", f"{gateway_url}/api/v1/projections/minibook/rebuild-receipts",
            expected=(200,), token=captain_token,
            payload=rebuild_receipt.model_dump(mode="json", by_alias=True),
        )
        finalization = PortalLiveRunFinalizationV1(
            decision_request_id=uuid4(), run_id=run_id, job_id=job_id,
            correlation_id=correlation_id,
            provider_trace_ids=tuple(item.trace_id for item in completions),
            restart_id=restart.restart_id, minibook_rebuild_id=rebuild_receipt.rebuild_id,
            policy_version="portal-live-v1", occurred_at=datetime.now(timezone.utc),
        )
        decision = _request_json(
            client, "POST", f"{gateway_url}/v1/control/portal-runs/{run_id}/decisions",
            expected=(200,), token=captain_token,
            payload=finalization.model_dump(mode="json", by_alias=True),
        )
        evidence = PortalLiveEvidenceV1.model_validate(
            _request_json(
                client, "POST", f"{gateway_url}/v1/control/evidence/query",
                expected=(200,), token=evidence_token,
                payload=PortalLiveEvidenceQueryV1(
                    run_id=run_id, job_id=job_id, correlation_id=correlation_id
                ).model_dump(mode="json"),
            )
        )
        terminal_surface: dict[str, object] = {}
        bearer_alias = bound_requirements[0].credential_alias
        for action in terminal_action_sequence():
            ticket = _ticket(
                client, gateway_url=gateway_url, job_id=job_id,
                access_token=access_token, alias=bearer_alias, action=action,
            )
            terminal_surface = _request_json(
                client, "POST",
                f"{gateway_url}/v1/portal/integration-setups/{job_id}/actions",
                expected=(200,), token=access_token,
                payload={**_ticket_payload(ticket, bearer_alias), "action": action},
            )
    if decision.get("status") != "accepted" or len(evidence.provider_traces) != 3:
        raise RuntimeError("aggregate release evidence was not accepted")
    terminal_actions = tuple(
        action for action in terminal_surface.get("actions", [])
        if isinstance(action, dict) and action.get("credential_alias") == bearer_alias
    )
    if len(terminal_actions) != 1 or terminal_actions[0].get("status") != "revoked":
        raise RuntimeError("terminal rotation/revoke lifecycle did not converge")
    summary: dict[str, object] = {
        "schema": "captain.portal-control-plane-live-evidence.v1",
        "status": "accepted",
        "job_id": str(job_id), "correlation_id": str(correlation_id),
        "run_id": run_id, "provider_trace_count": 3,
        "provider_kinds": list(provider_sequence()),
        "restart_ref": evidence.restart_ref.model_dump(mode="json"),
        "minibook_rebuild_ref": evidence.minibook_rebuild_ref.model_dump(mode="json"),
        "gateway_execution_ref": evidence.gateway_execution_ref.model_dump(mode="json"),
        "terminal_integration_status": "revoked",
        "terminal_setup_revision": terminal_surface.get("revision"),
        "secrets_emitted": False,
    }
    _atomic_json(evidence_path, summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-env", type=Path, default=Path(".env"))
    parser.add_argument("--n8n-credentials-env", type=Path, default=Path(".env.n8n-credentials"))
    parser.add_argument("--identity-env", type=Path, required=True)
    parser.add_argument(
        "--evidence", type=Path,
        default=Path(".captain-cook/evidence/portal-control-plane-live.json"),
    )
    args = parser.parse_args()
    result = run(
        root_env=args.root_env, n8n_credentials_env=args.n8n_credentials_env,
        identity_env=args.identity_env, evidence_path=args.evidence,
    )
    print(json.dumps({"schema": result["schema"], "status": result["status"], "secrets_emitted": False}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
