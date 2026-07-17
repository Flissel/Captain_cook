from __future__ import annotations

import os
from pathlib import Path
import shutil

import httpx
import pytest

from agenten.targets.n8n import N8nHttpClient, N8nTarget, SealedArtifact, ValidationCase


def _secret(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        env_path = Path(__file__).resolve().parents[3] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, candidate = line.partition("=")
            if separator and key.strip() == name and candidate.strip():
                return candidate.strip().strip('"').strip("'")
    return None


@pytest.mark.live
@pytest.mark.asyncio
async def test_gate_a_real_codex_n8n_gateway_trace() -> None:
    api_key = _secret("N8N_API_KEY")
    if api_key is None:
        pytest.skip(
            "Gate A prerequisite missing: N8N_API_KEY for existing VibeMind n8n"
        )
    if _secret("OPENAI_API_KEY") is None:
        pytest.skip("Gate A prerequisite missing: OPENAI_API_KEY for real Codex CLI")
    codex_cli = shutil.which("codex.cmd") or shutil.which("codex")
    if codex_cli is None:
        pytest.skip("Gate A prerequisite missing: official Codex CLI")
    gateway_run_id = os.environ.get("GATE_A_GATEWAY_RUN_ID")
    gateway_base_url = os.environ.get("GATE_A_GATEWAY_BASE_URL")
    gateway_reader_token = _secret("GATEWAY_READER_TOKEN")
    if not all((gateway_run_id, gateway_base_url, gateway_reader_token)):
        pytest.skip(
            "Gate A prerequisite missing: linked Gateway run configuration"
        )

    artifact = SealedArtifact(
        artifact_id="harmless-workflow",
        artifact_digest="a" * 64,
        namespace="captain-gate-a",
        workflow={
            "nodes": [
                {
                    "name": "Webhook",
                    "type": "n8n-nodes-base.webhook",
                    "typeVersion": 2,
                    "position": [0, 0],
                    "parameters": {
                        "path": "{{CAPTAIN_WEBHOOK_PATH}}",
                        "httpMethod": "POST",
                        "responseMode": "lastNode",
                    },
                },
                {
                    "name": "Respond",
                    "type": "n8n-nodes-base.code",
                    "typeVersion": 2,
                    "position": [240, 0],
                    "parameters": {
                        "jsCode": (
                            "return [{json:{...$json,"
                            "execution_id:$execution.id}}];"
                        )
                    },
                },
            ],
            "connections": {
                "Webhook": {
                    "main": [[{"node": "Respond", "type": "main", "index": 0}]]
                }
            },
            "settings": {},
        },
    )
    async with httpx.AsyncClient(timeout=30) as http:
        target = N8nTarget(
            N8nHttpClient(
                api_base_url="http://localhost:15678",
                webhook_base_url="http://localhost:15678",
                api_key=api_key,
                http=http,
            )
        )
        deployment = await target.deploy(artifact)
        evidence = await target.execute(
            deployment,
            ValidationCase(
                case_id="gate-a-harmless",
                correlation_id=gateway_run_id,
                input_payload={"operation": "ping"},
            ),
        )

        response = await http.get(
            (
                f"{gateway_base_url.rstrip('/')}/v1/projects/captain-gate-a/"
                f"runs/{gateway_run_id}/events"
            ),
            headers={"Authorization": f"Bearer {gateway_reader_token}"},
        )
        response.raise_for_status()
        events = response.json()

    serialized = str(events)
    assert evidence.workflow_id in serialized
    assert evidence.execution_id in serialized
    assert evidence.artifact_digest in serialized
    assert gateway_run_id in serialized
    assert any(
        event.get("trace", {}).get("session_id")
        and event.get("event_type") == "codex_session_finished"
        for event in events
    )
