from __future__ import annotations

import pytest
import httpx
import subprocess
import sys
from pathlib import Path

from agenten.provider_proxy.entrypoint import require_distinct_authority_secrets
from agenten.agent_runtime.runtime_entrypoint import (
    GatewayBackedRuntimeCostAuthority,
    ProviderProxyRuntimeFinalizer,
)
from tests.agent_runtime.test_service import (
    FakeCapabilityPolicy,
    FakeCodex,
    FakeCostAuthority,
    batch_for,
    resume_command_with_cost,
)


def test_proxy_upstream_and_gateway_secrets_must_be_distinct_from_runtime() -> None:
    safe = {
        "OPENAI_API_KEY": "openai-only",
        "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "proxy-only",
        "CAPTAIN_PROVIDER_RECEIPT_GATEWAY_TOKEN": "provider-writer-only",
        "CAPTAIN_RUNTIME_TOKEN": "runtime-only",
        "CAPTAIN_GATEWAY_TOKEN": "captain-only",
        "CAPTAIN_WORKER_GATEWAY_TOKEN": "worker-only",
    }
    require_distinct_authority_secrets(safe)
    with pytest.raises(ValueError, match="distinct"):
        require_distinct_authority_secrets(
            {**safe, "CAPTAIN_PROVIDER_PROXY_CLIENT_TOKEN": "provider-writer-only"}
        )


def test_required_proxy_secret_cannot_be_missing() -> None:
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        require_distinct_authority_secrets({})


def test_runner_script_can_load_package_from_scripts_directory() -> None:
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, str(root / "scripts" / "run_provider_budget_proxy.py"), "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_provider_proxy_package_module_exposes_real_entrypoint() -> None:
    root = Path(__file__).resolve().parents[2]
    completed = subprocess.run(
        [sys.executable, "-m", "agenten.provider_proxy.entrypoint", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--price-card" in completed.stdout


@pytest.mark.asyncio
async def test_runtime_finalizes_provider_before_gateway_cost_settlement() -> None:
    command = resume_command_with_cost()
    command = command.model_copy(
        update={
            "payload": command.payload.model_copy(
                update={"provider_context_sha256": "c" * 64}
            )
        }
    )
    authority = await FakeCostAuthority().authorize(command)
    grant = FakeCapabilityPolicy().derive(command, batch_for(command), command.occurred_at)
    result = FakeCodex(["command_accepted", "grant_recorded"])._result(command, grant)
    expected = await FakeCostAuthority().settle(command, result, authority)
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.port == 18091:
            order.append("provider-finalized")
            assert request.headers["X-Captain-Provider-Context-Ref"] == (
                "gateway://provider-budget-contexts/"
                + str(command.payload.provider_context_sha256)
            )
            return httpx.Response(200, json={"schema": "settlement"})
        order.append("runtime-cost-settled")
        return httpx.Response(200, json=expected.model_dump(mode="json", by_alias=True))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        finalizer = ProviderProxyRuntimeFinalizer(
            base_url="http://127.0.0.1:18091",
            token="runtime-only",
            client=client,
        )
        cost = GatewayBackedRuntimeCostAuthority(
            base_url="http://127.0.0.1:8090",
            token="captain-only",
            client=client,
            provider_finalizer=finalizer,
        )
        assert await cost.settle(command, result, authority) == expected
    assert order == ["provider-finalized", "runtime-cost-settled"]
