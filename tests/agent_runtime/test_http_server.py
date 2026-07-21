from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Protocol
from uuid import uuid5

import httpx
import pytest

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    AgentRuntimeResult,
    RuntimeStatus,
)
from agenten.agent_runtime.http_server import create_runtime_app
from agenten.agent_runtime.runtime_entrypoint import (
    GatewayBackedRuntimeState,
    RuntimeConfigurationError,
    RuntimeEntrypointSettings,
)
from tests.agent_runtime.test_service import batch_for
from tests.agent_runtime.test_gateway_client import command, result


TOKEN = "runtime-test-token"


class RuntimeExecutor(Protocol):
    async def execute(self, value: AgentRuntimeCommand) -> AgentRuntimeResult: ...


class RecordingExecutor:
    def __init__(self, response: AgentRuntimeResult | None = None) -> None:
        self.commands: list[AgentRuntimeCommand] = []
        self._response = response or result()

    async def execute(self, value: AgentRuntimeCommand) -> AgentRuntimeResult:
        self.commands.append(value)
        return self._response


async def _request(
    executor: RuntimeExecutor,
    *,
    method: str = "POST",
    path: str = "/v1/runtime/execute",
    authorization: str | None = f"Bearer {TOKEN}",
    payload: object | None = None,
) -> httpx.Response:
    headers = {}
    if authorization is not None:
        headers["Authorization"] = authorization
    transport = httpx.ASGITransport(
        app=create_runtime_app(executor=executor, token=TOKEN),
        raise_app_exceptions=False,
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://runtime.test") as client:
        return await client.request(
            method,
            path,
            headers=headers,
            json=(
                command().model_dump(mode="json", by_alias=True)
                if payload is None and method == "POST"
                else payload
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [None, "Basic abc", "Bearer", "Bearer wrong-runtime-token"],
)
async def test_execute_rejects_missing_malformed_and_wrong_bearer_tokens(
    authorization: str | None,
) -> None:
    executor = RecordingExecutor()

    response = await _request(executor, authorization=authorization)

    assert response.status_code == 401
    assert response.json() == {"detail": "runtime authentication failed"}
    assert executor.commands == []


@pytest.mark.asyncio
async def test_execute_rejects_unknown_command_fields_without_echoing_the_body() -> None:
    executor = RecordingExecutor()
    payload = command().model_dump(mode="json", by_alias=True)
    payload["unexpected_secret"] = "command-body-marker"

    response = await _request(executor, payload=payload)

    assert response.status_code == 422
    assert response.json() == {"detail": "invalid runtime command"}
    assert "command-body-marker" not in response.text
    assert executor.commands == []


@pytest.mark.asyncio
async def test_execute_replays_an_identical_command_with_the_same_typed_result() -> None:
    executor = RecordingExecutor()

    first = await _request(executor)
    second = await _request(executor)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == result().model_dump(mode="json", by_alias=True)
    assert executor.commands == [command(), command()]


@pytest.mark.asyncio
async def test_execute_returns_adapter_infrastructure_failure_as_a_typed_result() -> None:
    failed_value = copy.deepcopy(result().model_dump(mode="json", by_alias=True))
    failed_value.update(
        {
            "event_id": str(uuid5(command().event_id, "infrastructure-failure")),
            "status": RuntimeStatus.INFRASTRUCTURE_FAILED,
            "session_id": None,
            "artifact_refs": [],
            "evidence_refs": [],
            "error": "codex.run adapter failed",
        }
    )
    failed = AgentRuntimeResult.model_validate(failed_value)

    response = await _request(RecordingExecutor(failed))

    assert response.status_code == 200
    assert AgentRuntimeResult.model_validate(response.json()) == failed


@pytest.mark.asyncio
async def test_execute_does_not_log_or_return_token_or_command_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = "sensitive-command-body-marker"
    payload = command().model_dump(mode="json", by_alias=True)
    payload["unexpected"] = marker

    with caplog.at_level(logging.DEBUG):
        response = await _request(
            RecordingExecutor(),
            authorization="Bearer sensitive-runtime-token",
            payload=payload,
        )

    observable = response.text + caplog.text
    assert response.status_code == 401
    assert "sensitive-runtime-token" not in observable
    assert marker not in observable


@pytest.mark.asyncio
async def test_health_is_available_without_runtime_credentials() -> None:
    response = await _request(
        RecordingExecutor(),
        method="GET",
        path="/health",
        authorization=None,
    )

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_runtime_entrypoint_settings_are_strict_and_secret_redacting() -> None:
    settings = RuntimeEntrypointSettings.from_env(
        {
            "CAPTAIN_RUNTIME_TOKEN": "runtime-entrypoint-secret",
            "CAPTAIN_GATEWAY_TOKEN": "gateway-entrypoint-secret",
            "CAPTAIN_GATEWAY_URL": "http://127.0.0.1:8090",
            "CAPTAIN_RUNTIME_PORT": "8091",
        }
    )

    assert settings.host == "127.0.0.1"
    assert settings.port == 8091
    rendered = repr(settings) + settings.model_dump_json()
    assert "runtime-entrypoint-secret" not in rendered
    assert "gateway-entrypoint-secret" not in rendered

    with pytest.raises(RuntimeConfigurationError, match="missing required runtime settings"):
        RuntimeEntrypointSettings.from_env({})


@pytest.mark.asyncio
async def test_gateway_backed_state_loads_the_released_batch_with_captain_auth() -> None:
    expected = batch_for(command())
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=expected.model_dump(mode="json"),
            request=request,
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        state = GatewayBackedRuntimeState(
            base_url="http://gateway.test",
            token="gateway-state-secret",
            client=http,
        )
        observed = await state.get_released_batch(command())

    assert observed == expected
    assert requests[0].url.path == "/batches/batch-1/bundle"
    assert requests[0].headers["authorization"] == "Bearer gateway-state-secret"


def test_live_demo_service_script_manages_runtime_by_verified_identity() -> None:
    root = Path(__file__).parents[2]
    script = (root / "scripts" / "live-demo-services.ps1").read_text(encoding="utf-8")

    assert "runtime-demo.pid" in script
    assert "function Start-Runtime" in script
    assert "function Stop-ManagedRuntime" in script
    assert "PID no longer belongs to the managed Runtime process." in script
    assert "'-m','agenten.agent_runtime.runtime_entrypoint'" in script
    assert "CAPTAIN_RUNTIME_TOKEN" in script
    assert "CAPTAIN_RUNTIME_URL" in script
    assert "CAPTAIN_RUNTIME_PORT" in script
    assert "services=@('gateway','runtime'" in script


def test_example_environment_declares_runtime_boundary_without_secrets() -> None:
    root = Path(__file__).parents[2]
    example = (root / ".env.example").read_text(encoding="utf-8")

    assert "CAPTAIN_RUNTIME_TOKEN=\n" in example
    assert "CAPTAIN_RUNTIME_URL=http://127.0.0.1:8091\n" in example
    assert "CAPTAIN_RUNTIME_PORT=8091\n" in example
