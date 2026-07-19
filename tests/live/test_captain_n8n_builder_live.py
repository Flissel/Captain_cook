from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Iterator, Mapping
from uuid import uuid4

import httpx
import pytest

from agenten.agent_runtime.n8n_endpoint import (
    N8nEndpoint,
    N8nEndpointConfigurationError,
    resolve_n8n_endpoint,
)
from agenten.targets.n8n import (
    N8nDeployment,
    N8nHttpClient,
    N8nTarget,
    SealedArtifact,
    ValidationCase,
)


ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ContainerInvariant:
    container_id: str | None
    name: str | None
    state: str | None
    ports: str | None


def inspect_container(name: str) -> ContainerInvariant:
    """Read only a container's identity, state, and published ports."""

    if shutil.which("docker") is None:
        pytest.skip("Captain builder prerequisite missing: Docker CLI")
    result = subprocess.run(
        [
            "docker",
            "inspect",
            "--format",
            "{{.Id}}|{{.Name}}|{{.State.Status}}|{{json .NetworkSettings.Ports}}",
            name,
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if result.returncode != 0:
        return ContainerInvariant(None, None, None, None)
    parts = result.stdout.strip().split("|", maxsplit=3)
    if len(parts) != 4:
        pytest.fail("Docker returned an invalid safe container invariant")
    return ContainerInvariant(parts[0], parts[1], parts[2], parts[3])


def load_captain_builder_environment() -> dict[str, str]:
    """Load only the isolated builder connection fields without logging values."""

    values = {
        name: value
        for name in (
            "CAPTAIN_N8N_URL",
            "CAPTAIN_N8N_PORT",
            "CAPTAIN_N8N_API_KEY",
        )
        if (value := os.environ.get(name, "").strip())
    }
    env_file = ROOT / ".env.captain-n8n"
    if env_file.is_file():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            key = key.strip()
            if (
                separator
                and key in {
                    "CAPTAIN_N8N_URL",
                    "CAPTAIN_N8N_PORT",
                    "CAPTAIN_N8N_API_KEY",
                }
                and key not in values
                and value.strip()
            ):
                values[key] = value.strip().strip('"').strip("'")

    port = values.get("CAPTAIN_N8N_PORT", "")
    if "CAPTAIN_N8N_URL" not in values and port:
        values["CAPTAIN_N8N_URL"] = f"http://127.0.0.1:{port}"
    values["N8N_MODE"] = "captain-builder"
    return values


def _builder_endpoint_or_skip() -> N8nEndpoint:
    try:
        return resolve_n8n_endpoint(load_captain_builder_environment())
    except N8nEndpointConfigurationError as exc:
        pytest.skip(f"Captain builder prerequisite missing: {exc}")


async def health(api_base_url: str) -> int:
    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.get(f"{api_base_url}/healthz")
        return response.status_code


async def list_workflows(endpoint: N8nEndpoint) -> list[object] | None:
    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.get(
            f"{endpoint.api_base_url}/api/v1/workflows",
            params={"limit": 1},
            headers={"X-N8N-API-KEY": endpoint.api_key},
        )
        response.raise_for_status()
        body = response.json()
    if not isinstance(body, Mapping):
        return None
    data = body.get("data")
    return data if isinstance(data, list) else None


async def _require_ready_builder(endpoint: N8nEndpoint) -> None:
    try:
        if await health(endpoint.api_base_url) != 200:
            pytest.skip("Captain builder prerequisite missing: health endpoint")
        if await list_workflows(endpoint) is None:
            pytest.fail("Captain builder workflow API returned an invalid response")
    except httpx.HTTPError as exc:
        pytest.skip(
            "Captain builder prerequisite missing: authenticated API unavailable "
            f"({type(exc).__name__})"
        )


@pytest.fixture(autouse=True)
def preserve_vibemind_container() -> Iterator[ContainerInvariant]:
    before = inspect_container("vibemind-n8n")
    yield before
    assert inspect_container("vibemind-n8n") == before


def _respond_to_webhook_workflow() -> dict[str, object]:
    return {
        "nodes": [
            {
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 2,
                "position": [0, 0],
                "parameters": {
                    "httpMethod": "POST",
                    "path": "{{CAPTAIN_WEBHOOK_PATH}}",
                    "responseMode": "responseNode",
                    "options": {},
                },
            },
            {
                "name": "Evidence",
                "type": "n8n-nodes-base.code",
                "typeVersion": 2,
                "position": [240, 0],
                "parameters": {
                    "jsCode": (
                        "const payload = $input.first().json;\n"
                        "return [{ json: {\n"
                        "  execution_id: $execution.id,\n"
                        "  artifact_digest: payload.body.artifact_digest,\n"
                        "  correlation_id: payload.body.correlation_id,\n"
                        "} }];"
                    )
                },
            },
            {
                "name": "Respond to Webhook",
                "type": "n8n-nodes-base.respondToWebhook",
                "typeVersion": 1.4,
                "position": [480, 0],
                "parameters": {
                    "respondWith": "json",
                    "responseBody": "={{ $json }}",
                    "options": {},
                },
            },
        ],
        "connections": {
            "Webhook": {
                "main": [
                    [
                        {
                            "node": "Evidence",
                            "type": "main",
                            "index": 0,
                        }
                    ]
                ]
            },
            "Evidence": {
                "main": [
                    [
                        {
                            "node": "Respond to Webhook",
                            "type": "main",
                            "index": 0,
                        }
                    ]
                ]
            },
        },
        "settings": {"executionOrder": "v1"},
    }


async def _delete_exact_workflow(
    endpoint: N8nEndpoint,
    deployment: N8nDeployment,
) -> None:
    headers = {"X-N8N-API-KEY": endpoint.api_key}
    async with httpx.AsyncClient(
        base_url=endpoint.api_base_url,
        headers=headers,
        timeout=15,
    ) as http:
        deactivate = await http.post(
            f"/api/v1/workflows/{deployment.workflow_id}/deactivate"
        )
        deactivate.raise_for_status()
        deleted = await http.delete(f"/api/v1/workflows/{deployment.workflow_id}")
        deleted.raise_for_status()


@pytest.mark.live
@pytest.mark.asyncio
async def test_captain_builder_has_real_api_and_preserves_vibemind() -> None:
    endpoint = _builder_endpoint_or_skip()
    await _require_ready_builder(endpoint)

    assert await health(endpoint.api_base_url) == 200
    assert await list_workflows(endpoint) is not None


@pytest.mark.live
@pytest.mark.asyncio
async def test_captain_builder_deploys_executes_and_cleans_exact_workflow(
    record_property: Callable[[str, object], None],
) -> None:
    endpoint = _builder_endpoint_or_skip()
    await _require_ready_builder(endpoint)
    unique = uuid4().hex
    correlation_id = f"captain-live-smoke-{unique[:20]}"
    workflow = _respond_to_webhook_workflow()
    artifact_digest = hashlib.sha256(
        json.dumps(workflow, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    artifact = SealedArtifact(
        artifact_id=f"workflow-{unique[:16]}",
        artifact_digest=artifact_digest,
        namespace="live-smoke",
        workflow=workflow,
    )
    deployment: N8nDeployment | None = None
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            target = N8nTarget(N8nHttpClient.from_endpoint(endpoint, http))
            deployment = await target.deploy(artifact)
            execution = await target.execute(
                deployment,
                ValidationCase(
                    case_id=f"case-{unique[:16]}",
                    correlation_id=correlation_id,
                    input_payload={"operation": "ping"},
                ),
            )

        assert deployment.workflow_name.startswith("captain::live-smoke::")
        assert execution.workflow_id == deployment.workflow_id
        assert execution.execution_id
        assert execution.artifact_digest == artifact_digest
        assert execution.correlation_id == correlation_id
        record_property("n8n_target_identity", endpoint.api_base_url)
        record_property("workflow_id", deployment.workflow_id)
        record_property("execution_id", execution.execution_id)
        record_property("artifact_digest", artifact_digest)
        record_property("correlation_id", correlation_id)
    finally:
        primary_error = sys.exception()
        if deployment is not None:
            try:
                await _delete_exact_workflow(endpoint, deployment)
            except httpx.HTTPError as exc:
                message = (
                    "Captain live-smoke exact workflow cleanup failed: "
                    f"{type(exc).__name__}"
                )
                if primary_error is not None:
                    primary_error.add_note(message)
                else:
                    pytest.fail(message)
