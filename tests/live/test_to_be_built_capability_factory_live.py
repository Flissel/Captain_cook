from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from agenten.agent_factory.capability_factory_entrypoint import (
    CapabilityFactoryRunSummary,
    DockerCapabilitySandboxRunner,
)
from agenten.agent_factory.outcome_validation import CapabilitySandboxRequest
from agenten.agent_factory.outcome_contracts import FactoryTerminalDecision
from agenten.delivery.minibook_client import MinibookClient
from agenten.delivery.projection_cursor import ProjectionCursorStore
from agenten.delivery.projection_feed_client import GatewayProjectionFeedClient
from agenten.delivery.projector import MinibookProjector
from gateway.capability_catalog import CapabilityCatalogRecord
from gateway.contracts import CapabilityExecutionRecord


pytestmark = pytest.mark.live


def _required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if value:
        return value
    if os.environ.get("CAPABILITY_FACTORY_LIVE_REQUIRED") == "1":
        pytest.fail(f"live capability factory prerequisite is missing: {name}")
    pytest.skip(f"live capability factory prerequisite is missing: {name}")


@pytest.mark.asyncio
async def test_pinned_capability_sandbox_runs_real_isolated_import_and_pytest(
    tmp_path: Path,
) -> None:
    image = _required_environment("CAPTAIN_CAPABILITY_SANDBOX_IMAGE")
    (tmp_path / "capability").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "capability" / "team.py").write_text(
        "CAPABILITY_ID = 'sandbox-live-smoke'\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_team.py").write_text(
        "from capability.team import CAPABILITY_ID\n\n"
        "def test_identity(): assert CAPABILITY_ID == 'sandbox-live-smoke'\n",
        encoding="utf-8",
    )
    entries = []
    for path in tmp_path.rglob("*"):
        if path.is_file():
            content = path.read_bytes()
            entries.append(
                (
                    path.relative_to(tmp_path).as_posix(),
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                )
            )
    tree_digest = hashlib.sha256(
        json.dumps(sorted(entries), separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    execution_id = UUID("a06a2896-7b72-4d8f-88e9-55446f8bac3d")
    result = await asyncio.wait_for(
        DockerCapabilitySandboxRunner(image=image).validate(
            CapabilitySandboxRequest(
                request_digest="d" * 64,
                execution_id=execution_id,
                process_identity=f"sandbox-handle://{execution_id}",
                correlation_id=execution_id,
                workspace=tmp_path,
                python_path_root=tmp_path,
                module_names=("capability.team",),
                test_paths=("tests/test_team.py",),
                extracted_tree_sha256=tree_digest,
                package_archive_sha256="e" * 64,
                timeout_seconds=60,
            )
        ),
        timeout=75,
    )

    assert result.status == "passed"
    assert result.workspace_was_read_only is True
    assert result.network_was_disabled is True
    assert result.resource_limits_were_enforced is True


@pytest.mark.asyncio
async def test_provider_backed_capability_factory_release_and_projection(
    tmp_path: Path,
) -> None:
    manifest_path = Path(_required_environment("CAPABILITY_FACTORY_LIVE_MANIFEST"))
    gateway_url = _required_environment("CAPTAIN_GATEWAY_URL").rstrip("/")
    gateway_token = _required_environment("CAPTAIN_GATEWAY_TOKEN")
    runtime_url = _required_environment("CAPTAIN_RUNTIME_URL").rstrip("/")
    minibook_url = _required_environment("MINIBOOK_BACKEND_URL").rstrip("/")
    minibook_api_key = _required_environment("MINIBOOK_API_KEY")
    projection_api_key = _required_environment("MINIBOOK_PROJECTION_API_KEY")

    content = manifest_path.read_bytes()
    assert manifest_path.stem == hashlib.sha256(content).hexdigest()
    manifest = json.loads(content)
    assert manifest["schema"] == "captain.capability-factory-evidence-manifest.v1"
    summary = CapabilityFactoryRunSummary.model_validate(manifest["summary"])
    assert summary.terminal_state == "ready_to_use"
    assert summary.execution_state == "completed"
    assert summary.release_authority_job_id is not None
    if summary.execution_mode == "created":
        assert summary.release_authority_job_id == summary.invocation_job_id
        assert summary.creation_job_id is not None
        assert summary.recovery_id is not None
        assert len(summary.e2e_batch_ids) == 3
        assert len(set(summary.e2e_batch_ids)) == 3
        assert len(summary.release_evidence_sha256) == 4
    else:
        assert summary.execution_mode == "reused"
        assert summary.creation_job_id is None
        assert summary.recovery_id is None
        assert summary.e2e_batch_ids == ()
        assert summary.release_evidence_sha256 == ()
    assert summary.capability_version is not None
    assert summary.execution_command_id is not None
    assert summary.execution_result_id is not None
    assert summary.projection_event_ids

    headers = {"Authorization": f"Bearer {gateway_token}"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        terminal_response = await client.get(
            f"{gateway_url}/v1/factory/terminal-decisions/{summary.release_authority_job_id}",
            headers=headers,
        )
        terminal_response.raise_for_status()
        terminal = FactoryTerminalDecision.model_validate(terminal_response.json())
        assert terminal.decision_id == summary.terminal_decision_id
        assert terminal.state == "ready_to_use"

        capability_response = await client.get(
            f"{gateway_url}/v1/capabilities/{summary.capability_id}",
            params={"version": summary.capability_version},
            headers=headers,
        )
        capability_response.raise_for_status()
        capability = CapabilityCatalogRecord.model_validate(capability_response.json())
        assert capability.status == "ready_to_use"
        assert capability.capability_version == summary.capability_version
        assert capability.release_authority_job_id == summary.release_authority_job_id
        assert capability.package_ref.sha256 == summary.package_sha256

        execution_response = await client.get(
            f"{gateway_url}/v1/capability-executions/{summary.execution_command_id}",
            headers=headers,
        )
        execution_response.raise_for_status()
        execution = CapabilityExecutionRecord.model_validate(execution_response.json())
        assert execution.result_id == summary.execution_result_id
        assert execution.status == "succeeded"

        feed = GatewayProjectionFeedClient(gateway_url, gateway_token, client)
        projection_events = await feed.events_for_correlation(summary.correlation_id)

        runtime_health = await client.get(f"{runtime_url}/health")
        runtime_health.raise_for_status()
        minibook_health = await client.get(f"{minibook_url}/health")
        minibook_health.raise_for_status()

    event_ids = {event.event_id for event in projection_events}
    assert set(summary.projection_event_ids).issubset(event_ids)
    assert any(
        event.event_id in set(summary.projection_event_ids)
        and event.correlation_id == summary.correlation_id
        and event.event_id == summary.execution_result_id
        and event.causation_id == summary.execution_command_id
        and event.event_type == "codex.result"
        and event.payload.status_id == "built"
        for event in projection_events
    )

    minibook = MinibookClient(
        minibook_url,
        minibook_api_key,
        projection_api_key=projection_api_key,
    )
    try:
        first = MinibookProjector(
            minibook,
            ProjectionCursorStore(tmp_path / "projection-rebuild.sqlite3"),
            owner_id="capability-factory-live-rebuild",
        ).rebuild(projection_events)
        second = MinibookProjector(
            minibook,
            ProjectionCursorStore(tmp_path / "projection-rebuild.sqlite3"),
            owner_id="capability-factory-live-replay",
        ).rebuild(projection_events)
        assert first
        assert all(result.outcome in {"projected", "duplicate"} for result in first)
        assert all(result.outcome == "duplicate" for result in second)
        project = next(
            item
            for item in minibook.list_projects()
            if item["name"] == MinibookProjector.PROJECTION_PROJECT
        )
        readback = json.dumps(minibook.list_posts(project["id"]), sort_keys=True)
        assert str(summary.correlation_id) in readback
        assert str(UUID(str(summary.execution_result_id))) in readback
    finally:
        minibook.close()
