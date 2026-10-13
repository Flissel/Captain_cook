"""Minibook API client — HTTP helpers with retry, credential storage, agent registration."""

import asyncio
import json
import os
import re
from typing import TypeVar
from uuid import UUID
import aiohttp
from pydantic import BaseModel

from .contracts import (
    CreationJobV3,
    CreationProgressV1,
    CreationResultV2,
    CreationSubmissionReceipt,
    FactoryBuildAssignmentV2,
    FactoryBuildEffectV2,
)

from .constants import MINIBOOK_URL, CREDS_FILE

MAX_RETRIES = 3
RETRY_DELAY = 2  # seconds


async def _request_with_retry(method: str, session: aiohttp.ClientSession,
                               path: str, headers: dict, **kwargs):
    """Execute HTTP request with retry on connection errors."""
    url = f"{MINIBOOK_URL}{path}"
    for attempt in range(MAX_RETRIES):
        try:
            async with getattr(session, method)(url, headers=headers, **kwargs) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    raise Exception(f"API {resp.status} {method.upper()} {path}: {body}")
                if resp.status == 204:
                    return None
                return await resp.json()
        except (aiohttp.ClientOSError, aiohttp.ServerDisconnectedError,
                aiohttp.ClientConnectorError, ConnectionResetError) as e:
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(RETRY_DELAY * (attempt + 1))
            else:
                raise


async def api_post(session: aiohttp.ClientSession, path: str, data: dict, api_key: str = None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return await _request_with_retry("post", session, path, headers, json=data)


async def api_put(session: aiohttp.ClientSession, path: str, data: dict, api_key: str = None):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return await _request_with_retry("put", session, path, headers, json=data)


async def api_get(session: aiohttp.ClientSession, path: str, api_key: str = None, params: dict = None):
    headers = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return await _request_with_retry("get", session, path, headers, params=params)


ModelT = TypeVar("ModelT", bound=BaseModel)


class CreationArtifactMetadata(BaseModel):
    sha256: str
    size_bytes: int


def _require_json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError("Minibook response must be a JSON object")
    return value


def _require_model(value: object, model: type[ModelT]) -> ModelT:
    if isinstance(value, model):
        return value
    if isinstance(value, BaseModel):
        raise TypeError(f"expected {model.__name__}")
    if not isinstance(value, dict):
        raise TypeError(f"expected {model.__name__}")
    return model.model_validate(_require_json_object(value))


async def submit_creation_job_v3(
    session: aiohttp.ClientSession,
    job: CreationJobV3,
    *,
    api_key: str,
) -> CreationSubmissionReceipt:
    typed_job = _require_model(job, CreationJobV3)
    payload = await api_post(
        session,
        "/v3/creation/jobs",
        typed_job.model_dump(mode="json", by_alias=True),
        api_key=api_key,
    )
    return CreationSubmissionReceipt.model_validate(_require_json_object(payload))


async def creation_status_v3(
    session: aiohttp.ClientSession, job_id: UUID, *, api_key: str
) -> CreationProgressV1:
    payload = await api_get(session, f"/v3/creation/jobs/{job_id}", api_key=api_key)
    return CreationProgressV1.model_validate(_require_json_object(payload))


async def creation_result_v3(
    session: aiohttp.ClientSession, job_id: UUID, *, api_key: str
) -> CreationResultV2:
    payload = await api_get(session, f"/v3/creation/jobs/{job_id}/result", api_key=api_key)
    return CreationResultV2.model_validate(_require_json_object(payload))


async def cancel_creation_v3(
    session: aiohttp.ClientSession,
    job_id: UUID,
    *,
    expected_version: int,
    api_key: str,
) -> CreationProgressV1:
    payload = await api_post(
        session,
        f"/v3/creation/jobs/{job_id}/cancel",
        {"expected_version": expected_version},
        api_key=api_key,
    )
    return CreationProgressV1.model_validate(_require_json_object(payload))


async def next_build_assignment(
    session: aiohttp.ClientSession, *, worker_token: str
) -> FactoryBuildAssignmentV2 | None:
    payload = await api_get(
        session, "/v3/creation/assignments/next", api_key=worker_token
    )
    if payload is None:
        return None
    return FactoryBuildAssignmentV2.model_validate(_require_json_object(payload))


async def claim_build_assignment(
    session: aiohttp.ClientSession,
    assignment_id: UUID,
    *,
    worker_id: str,
    worker_token: str,
) -> FactoryBuildAssignmentV2:
    payload = await api_post(
        session,
        f"/v3/creation/assignments/{assignment_id}/claim",
        {"worker_id": worker_id},
        api_key=worker_token,
    )
    return FactoryBuildAssignmentV2.model_validate(_require_json_object(payload))


async def submit_build_effect(
    session: aiohttp.ClientSession,
    effect: FactoryBuildEffectV2,
    *,
    worker_token: str,
) -> CreationProgressV1:
    typed_effect = _require_model(effect, FactoryBuildEffectV2)
    payload = await api_post(
        session,
        f"/v3/creation/assignments/{typed_effect.assignment_id}/effect",
        typed_effect.model_dump(mode="json", by_alias=True),
        api_key=worker_token,
    )
    return CreationProgressV1.model_validate(_require_json_object(payload))


async def creation_artifact_metadata(
    session: aiohttp.ClientSession, sha256: str, *, worker_token: str
) -> CreationArtifactMetadata:
    return CreationArtifactMetadata.model_validate(_require_json_object(
        await api_get(
            session,
            f"/v3/creation/artifacts/{sha256}/metadata",
            api_key=worker_token,
        )
    ))


# --- Credential Storage ---

def load_credentials() -> dict:
    if CREDS_FILE.exists():
        return json.loads(CREDS_FILE.read_text())
    return {}


def save_credentials(creds: dict):
    CREDS_FILE.write_text(json.dumps(creds, indent=2))


async def register_agent(session: aiohttp.ClientSession, name: str, creds: dict) -> dict:
    credential_key, remote_name = _agent_identity(name)
    if credential_key in creds:
        print(f"  [=] Loaded {name}")
        return creds[credential_key]
    try:
        result = await api_post(session, "/api/v1/agents", {"name": remote_name})
        print(f"  [+] Registered {name}")
        creds[credential_key] = result
        save_credentials(creds)
        return result
    except Exception as e:
        if "already taken" in str(e):
            raise Exception(f"Agent {name} exists but no saved key. Delete swarm_agents.json and minibook.db.")
        raise


def _agent_identity(name: str) -> tuple[str, str]:
    """Scope credentials and Minibook names for concurrent factory runs."""

    suffix = os.environ.get("MINIBOOK_SWARM_AGENT_SUFFIX", "").strip()
    if not suffix:
        return name, name
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", suffix):
        raise ValueError("MINIBOOK_SWARM_AGENT_SUFFIX must be a safe identifier")
    return f"{name}::{suffix}", f"{name} [{suffix}]"


# --- Agent Registry ---

# Team-key → community group mapping
COMMUNITY_GROUPS = {
    "core":          "sales_core",
    "outreach":      "outreach",
    "bdr":           "outreach",
    "intel":         "intelligence",
    "research":      "intelligence",
    "qualification": "qualification",
    "revops":        "operations",
    "callintel":     "operations",
    "workspace":     "content",
    "content":       "content",
    "wiring":        "sales_core",
}

COMMUNITY_PROJECTS = {
    "sales_core":     "Community: Sales Core Team",
    "outreach":       "Community: Channel Outreach",
    "intelligence":   "Community: Competitive Intel & Research",
    "qualification":  "Community: Lead Qualification",
    "operations":     "Community: RevOps & Call Intelligence",
    "content":        "Community: Content & Workspace",
}


async def ensure_community_project(
    session: aiohttp.ClientSession,
    team_key: str,
    registry_agent_api_key: str,
) -> str | None:
    """Get or create the community project for a team_key. Returns project_id."""
    group = COMMUNITY_GROUPS.get(team_key)
    if not group:
        return None
    project_name = COMMUNITY_PROJECTS[group]

    # Try GET all projects and find by name
    try:
        projects = await api_get(session, "/api/v1/projects", api_key=registry_agent_api_key)
        for p in projects:
            if p["name"] == project_name:
                return p["id"]
    except Exception as e:
        # Only proceed to creation on network/timeout errors, not auth failures
        if any(code in str(e) for code in ("401", "403", "500")):
            print(f"  [Community] GET projects failed ({e}) — skipping creation")
            return None

    # Create new community project
    try:
        proj = await api_post(session, "/api/v1/projects", {
            "name": project_name,
            "description": f"Validated agents for {group.replace('_', ' ').title()} capabilities.",
        }, api_key=registry_agent_api_key)
        print(f"  [Community] Created project: {project_name}")
        return proj["id"]
    except Exception as e:
        print(f"  [Community] Could not create project {project_name}: {e}")
        return None


async def register_agent_in_registry(
    session: aiohttp.ClientSession,
    team_key: str,
    run_id: str,
    eval_score: int,
    eval_reason: str,
    status: str,                     # "candidate" or "validated"
    output_dir: str | None,
    mcp_servers: list[str],
    capabilities: list[str],
    tools_py_path: str | None,
    agent_name: str | None,
    registry_agent_api_key: str | None,
    todo_status: str = "pending",
) -> dict | None:
    """Register (or update) a team in the agent registry.

    Called twice per successful team:
    1. After mock eval PASS → status='candidate'
    2. After real eval PASS (post-TODO impl) → status='validated'
    """
    try:
        payload = {
            "team_key": team_key,
            "run_id": run_id,
            "eval_score": eval_score,
            "eval_reason": eval_reason,
            "status": status,
            "todo_status": todo_status,
            "output_dir": output_dir,
            "tools_py_path": tools_py_path,
            "mcp_servers": mcp_servers,
            "capabilities": capabilities,
            "agent_name": agent_name,
        }
        entry = await api_post(session, "/api/v1/registry", payload)
        print(f"  [Registry] {team_key} registered as {status} (score={eval_score})")

        # If validated + community enabled → join community project
        if status == "validated" and registry_agent_api_key and agent_name:
            project_id = await ensure_community_project(session, team_key, registry_agent_api_key)
            if project_id and entry.get("id"):
                # Join community project with role "validated-agent"
                try:
                    await api_post(
                        session,
                        f"/api/v1/projects/{project_id}/join",
                        {"role": "validated-agent"},
                        api_key=registry_agent_api_key,
                    )
                    print(f"  [Registry] RegistryAgent joined community project {project_id} for {agent_name}")
                except Exception as e:
                    if "Already" not in str(e):
                        print(f"  [Registry] Community join failed: {e}")
                # Write community_project_id back to registry entry (I1 fix)
                try:
                    await api_put(
                        session,
                        f"/api/v1/registry/{entry['id']}/status",
                        {"status": status, "community_project_id": project_id},
                        api_key=registry_agent_api_key,
                    )
                except Exception as e:
                    print(f"  [Registry] Could not update community_project_id: {e}")
                # Post summary to community project
                try:
                    caps = ", ".join(capabilities) if capabilities else "general"
                    servers = ", ".join(mcp_servers) if mcp_servers else "none"
                    await api_post(
                        session,
                        f"/api/v1/projects/{project_id}/posts",
                        {
                            "title": f"Validated: {agent_name} ({team_key})",
                            "content": (
                                f"## New Validated Agent Team\n\n"
                                f"**Team:** {team_key}\n"
                                f"**Agent:** {agent_name}\n"
                                f"**Eval Score:** {eval_score}/10\n"
                                f"**MCP Servers:** {servers}\n"
                                f"**Capabilities:** {caps}\n"
                                f"**TODO Status:** {todo_status}\n\n"
                                f"**Eval Summary:** {eval_reason[:300]}\n\n"
                                f"Output: `{output_dir}`"
                            ),
                        },
                        api_key=registry_agent_api_key,
                    )
                    print(f"  [Registry] Posted summary to community project for {agent_name}")
                except Exception as e:
                    print(f"  [Registry] Summary post failed: {e}")
        return entry
    except Exception as e:
        print(f"  [Registry] Registration failed for {team_key}: {e}")
        return None

