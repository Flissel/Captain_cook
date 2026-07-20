from __future__ import annotations

import pytest

from minibook.swarm import api_client


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
