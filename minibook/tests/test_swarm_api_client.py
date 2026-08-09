from __future__ import annotations

from pathlib import Path
import pytest

from minibook.swarm import api_client
from minibook.swarm.contracts import CreationJobV3, FactoryBuildAssignmentV2


@pytest.mark.asyncio
async def test_register_agent_namespaces_remote_identity_and_cached_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MINIBOOK_SWARM_AGENT_SUFFIX", "factory-run-01")
    captured: dict[str, object] = {}

    async def fake_post(_session: object, _path: str, data: dict[str, object]) -> dict[str, str]:
        captured.update(data)
        return {"id": "agent-1", "api_key": "local-test-key"}

    monkeypatch.setattr(api_client, "api_post", fake_post)
    monkeypatch.setattr(api_client, "save_credentials", lambda _creds: None)
    credentials: dict[str, object] = {}

    registered = await api_client.register_agent(object(), "SwarmManager", credentials)

    assert captured == {"name": "SwarmManager [factory-run-01]"}
    assert credentials == {"SwarmManager::factory-run-01": registered}


@pytest.mark.asyncio
async def test_v3_client_methods_narrow_unknown_payloads_to_typed_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = Path(__file__).parents[2] / "tests/fixtures/contracts/minibook_creation_job.v3.json"
    job = CreationJobV3.model_validate_json(fixture.read_text(encoding="utf-8"))
    captured: list[tuple[str, str]] = []

    async def fake_post(_session: object, path: str, data: dict[str, object], api_key: str | None = None) -> object:
        captured.append((path, api_key or ""))
        return {"creation_job_id": str(job.creation_job_id), "status": "queued", "subject_version": 1, "replayed": False}

    monkeypatch.setattr(api_client, "api_post", fake_post)
    receipt = await api_client.submit_creation_job_v3(object(), job, api_key="captain-token")
    assert receipt.creation_job_id == job.creation_job_id
    assert captured == [("/v3/creation/jobs", "captain-token")]

    async def malformed_get(*_args: object, **_kwargs: object) -> object:
        return ["not", "an", "assignment"]

    monkeypatch.setattr(api_client, "api_get", malformed_get)
    with pytest.raises(ValueError, match="JSON object"):
        await api_client.next_build_assignment(object(), worker_token="worker-token")


def test_v3_client_rejects_wrong_contract_type_before_network() -> None:
    with pytest.raises(TypeError, match="CreationJobV3"):
        api_client._require_model(object(), CreationJobV3)
