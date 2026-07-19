"""Live evidence for Captain's revocable n8n MCP broker.

The test deliberately creates no n8n workflow.  Its upstream effect is limited
to MCP capability discovery on Captain's isolated builder, while all lifecycle
authority is held by an ephemeral local Gateway backed by TEST_MARIADB_DSN.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from agenten.agent_runtime.contracts import (
    AgentRuntimeCommand,
    CapabilityGrant,
    CapabilityGrantRevocation,
)
from agenten.agent_runtime.gateway_client import GatewayRuntimeClient
from agenten.agent_runtime.n8n_mcp_broker import McpLeaseIssuer
from agenten.delivery.codex_runs import GatewayCodexRunRepository
from agenten.delivery.gateway_client import GatewayDeliveryClient
from agenten.execution.codex_policy import AuthorizedCodexRun, FrozenEnvironment
from agenten.execution.codex_supervisor import (
    CodexRunRequest,
    CodexRunResult,
    CodexSupervisor,
)
from agenten.execution.process import PackageExecutionStatus


ROOT = Path(__file__).resolve().parents[2]
HERMES_ROOT = ROOT / "hermes-agent"
sys.path.insert(0, str(HERMES_ROOT))

from hermes_cli.n8n_worker_mcp import (  # noqa: E402
    HermesGenericMcpTransport,
    N8nWorkerMcp,
)


pytestmark = [pytest.mark.live]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _builder_environment() -> dict[str, str]:
    path = ROOT / ".env.captain-n8n"
    if not path.is_file():
        pytest.fail("required live gate: Captain builder environment is unavailable")
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        name, separator, value = line.partition("=")
        if not separator:
            pytest.fail("required live gate: Captain builder environment is invalid")
        values[name.strip()] = value.strip()
    for name in (
        "CAPTAIN_N8N_MCP_BROKER_URL",
        "CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET",
        "CAPTAIN_N8N_PORT",
    ):
        if not values.get(name):
            pytest.fail(f"required live gate: {name} is unavailable")
    return values


def _runtime_command(*, unique: str, now: datetime) -> AgentRuntimeCommand:
    subtask_id = f"subtask-{unique}"
    return AgentRuntimeCommand.model_validate(
        {
            "schema": "captain.agent-runtime-command.v1",
            "event_id": str(uuid4()),
            "correlation_id": str(uuid4()),
            "occurred_at": now.isoformat(),
            "producer": "captain",
            "subject_id": subtask_id,
            "subject_version": 1,
            "payload": {
                "operation": "codex.run",
                "project_id": f"broker-{unique}",
                "batch_id": f"batch-{unique}",
                "subtask_id": subtask_id,
                "workspace_ref": f"workspace://broker/{unique}",
                "prompt_ref": {
                    "uri": f"artifact://broker/prompts/{unique}",
                    "sha256": "a" * 64,
                    "media_type": "text/markdown",
                },
                "integration_intent": "n8n",
                "capability_profile": "n8n-builder",
                "limits": {"wall_seconds": 60, "max_iterations": 1},
            },
        }
    )


def _grant(command: AgentRuntimeCommand, now: datetime) -> CapabilityGrant:
    payload = command.payload
    assert payload.batch_id is not None
    assert payload.subtask_id is not None
    assert payload.workspace_ref is not None
    return CapabilityGrant.model_validate(
        {
            "schema": "captain.capability-grant.v1",
            "grant_id": f"grant-{command.event_id.hex}",
            "command_id": str(command.event_id),
            "batch_id": payload.batch_id,
            "batch_version": 1,
            "subtask_id": payload.subtask_id,
            "workspace_ref": payload.workspace_ref,
            "profile": "n8n-builder",
            "capabilities": [
                "codex.cancel",
                "codex.heartbeat",
                "codex.resume",
                "codex.run",
                "codex.status",
                "mcp.n8n",
                "tests.run",
                "workspace.write",
            ],
            "mcp_servers": ["n8n-mcp"],
            "issued_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
        }
    )


def _start_gateway(
    *,
    dsn: str,
    token: str,
    port: int,
    worker_token: str | None = None,
) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment.update(
        {
            "LEDGER_DSN": dsn,
            "CAPTAIN_GATEWAY_TOKEN": token,
            "WORKER_GATEWAY_TOKEN": worker_token or secrets.token_urlsafe(32),
            "GATEWAY_PORT": str(port),
        }
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "gateway.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "error",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_for_gateway(base_url: str) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(60):
            try:
                if (await client.get(f"{base_url}/healthz")).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    pytest.fail("required live gate: ephemeral Gateway did not become ready")


def _broker_is_running() -> bool:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.captain-n8n",
            "--profile",
            "mcp-broker",
            "-f",
            "docker-compose.captain-n8n.yml",
            "ps",
            "-q",
            "mcp-broker",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail("required live gate: could not inspect Captain MCP broker state")
    return bool(result.stdout.strip())


def _start_broker(*, gateway_port: int, gateway_token: str) -> None:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        pytest.fail("required live gate: PowerShell is unavailable")
    environment = os.environ.copy()
    environment.update(
        {
            "CAPTAIN_GATEWAY_URL": f"http://host.docker.internal:{gateway_port}",
            "CAPTAIN_GATEWAY_TOKEN": gateway_token,
        }
    )
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            "scripts/captain-n8n.ps1",
            "broker-start",
        ],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        pytest.fail("required live gate: Captain MCP broker did not start")


def _stop_broker() -> None:
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--env-file",
            ".env.captain-n8n",
            "--profile",
            "mcp-broker",
            "-f",
            "docker-compose.captain-n8n.yml",
            "stop",
            "mcp-broker",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        pytest.fail("Captain MCP broker cleanup failed")


async def _wait_for_broker(url: str) -> None:
    async with httpx.AsyncClient(timeout=1) as client:
        for _ in range(60):
            try:
                response = await client.post(f"{url}/mcp-server/http", json={})
                if response.status_code == 403:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    pytest.fail("required live gate: Captain MCP broker did not become ready")


@pytest.mark.asyncio
async def test_captain_mcp_broker_revocation_is_enforced_by_live_gateway(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An accepted gateway grant reaches n8n once and is denied after revocation."""

    dsn = os.environ.get("TEST_MARIADB_DSN", "").strip()
    if not dsn:
        pytest.fail("required live gate: TEST_MARIADB_DSN is unavailable")
    builder = _builder_environment()
    broker_url = builder["CAPTAIN_N8N_MCP_BROKER_URL"].rstrip("/")
    if _broker_is_running():
        pytest.fail("required live gate: Captain MCP broker is already running")

    now = datetime.now(timezone.utc)
    unique = uuid4().hex[:24]
    command = _runtime_command(unique=unique, now=now)
    grant = _grant(command, now)
    gateway_token = secrets.token_urlsafe(32)
    gateway_port = _free_port()
    gateway_base = f"http://127.0.0.1:{gateway_port}"
    gateway_process = _start_gateway(
        dsn=dsn,
        token=gateway_token,
        port=gateway_port,
    )
    broker_started = False
    try:
        await _wait_for_gateway(gateway_base)
        async with httpx.AsyncClient(timeout=20) as client:
            batch = {
                "batch_id": command.payload.batch_id,
                "title": "Captain MCP broker live gate",
                "goal": "Discover isolated n8n MCP capabilities once",
                "subtask_ids": [command.payload.subtask_id],
                "target": "n8n",
                "capability_tags": ["n8n-builder"],
                "depends_on": [],
                "constraints": [],
                "acceptance_criteria": [
                    {
                        "assertion_id": "broker-revocation",
                        "kind": "status_equals",
                        "expected": "succeeded",
                    }
                ],
                "golden_cases": [],
            }
            created = await client.post(
                f"{gateway_base}/blocks",
                headers={"Authorization": f"Bearer {gateway_token}"},
                json={"block_type": "work_batch", "status": "pending", "data": batch},
            )
            assert created.status_code in {200, 201}, "Gateway rejected the live batch"
            gateway = GatewayRuntimeClient(gateway_base, gateway_token, client)
            await gateway.accept_runtime_command(command)
            await gateway.record_capability_grant(grant)

            _start_broker(gateway_port=gateway_port, gateway_token=gateway_token)
            broker_started = True
            await _wait_for_broker(broker_url)

            issuer = McpLeaseIssuer(builder["CAPTAIN_N8N_MCP_BROKER_SIGNING_SECRET"])
            token = issuer.issue(grant, command, broker_url, now)
            monkeypatch.setenv("N8N_MCP_TOKEN", token)
            configured = {
                "n8n-mcp": {
                    "url": f"{broker_url}/mcp-server/http",
                    "headers": {"Authorization": "Bearer ${N8N_MCP_TOKEN}"},
                    "tools": {"include": ["search_workflows"]},
                    "timeout": 45,
                    "enabled": True,
                }
            }
            mcp = N8nWorkerMcp(
                grant=grant,
                configured_servers=configured,
                transport=HermesGenericMcpTransport(),
                clock=lambda: now,
            )
            assert await asyncio.to_thread(mcp.discover_capabilities) == (
                "search_workflows",
            )

            revocation = CapabilityGrantRevocation(
                schema_name="captain.capability-grant-revocation.v1",
                revocation_id=uuid4(),
                grant_id=grant.grant_id,
                command_id=command.event_id,
                revoked_at=now + timedelta(seconds=1),
                reason="captain_cancelled",
            )
            await gateway.record_capability_grant_revocation(revocation)
            denied = await client.post(
                f"{broker_url}/mcp-server/http",
                headers={"Authorization": f"Bearer {token}"},
                json={"jsonrpc": "2.0", "id": "revoked-live-gate", "method": "tools/list"},
            )
            assert denied.status_code == 403
    finally:
        if broker_started:
            _stop_broker()
        gateway_process.terminate()
        try:
            gateway_process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            gateway_process.kill()
            gateway_process.wait(timeout=15)


class _RestartPolicy:
    """Authorize a request only to prove the persisted session blocks a runner."""

    def authorize(self, request: CodexRunRequest) -> AuthorizedCodexRun:
        return AuthorizedCodexRun(
            workspace=request.workspace.resolve(),
            command=request.command,
            environment=FrozenEnvironment({}),
        )


class _RunnerThatMustNotStart:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self, authorized: AuthorizedCodexRun) -> CodexRunResult:
        del authorized
        self.calls += 1
        raise AssertionError("Gateway recovery started a duplicate Codex provider run")


class _UnusedLegacyEvidenceWriter:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"legacy evidence writer used during recovery: {name}")


@pytest.mark.asyncio
async def test_gateway_restart_fences_an_active_codex_session_before_provider_start(
    tmp_path: Path,
) -> None:
    """A new supervisor process must not duplicate a Gateway-persisted session."""

    dsn = os.environ.get("TEST_MARIADB_DSN", "").strip()
    if not dsn:
        pytest.fail("required live gate: TEST_MARIADB_DSN is unavailable")
    unique = uuid4().hex[:16]
    batch_id = f"restart-{unique}"
    worker_id = f"worker-{unique[:12]}"
    project_id = f"project-{unique}"
    run_id = f"run-{unique}"
    gateway_token = secrets.token_urlsafe(32)
    worker_token = secrets.token_urlsafe(32)
    gateway_port = _free_port()
    gateway_base = f"http://127.0.0.1:{gateway_port}"
    process = _start_gateway(
        dsn=dsn,
        token=gateway_token,
        worker_token=worker_token,
        port=gateway_port,
    )
    try:
        await _wait_for_gateway(gateway_base)
        async with httpx.AsyncClient(timeout=20) as http:
            batch = {
                "batch_id": batch_id,
                "title": "Gateway restart session fence",
                "goal": "Prove an active Codex session is never launched twice",
                "subtask_ids": [f"subtask-{unique}"],
                "target": "codex",
                "capability_tags": ["code-builder"],
                "depends_on": [],
                "constraints": [],
                "acceptance_criteria": [
                    {
                        "assertion_id": "no-duplicate-provider",
                        "kind": "status_equals",
                        "expected": "succeeded",
                    }
                ],
                "golden_cases": [],
            }
            created = await http.post(
                f"{gateway_base}/blocks",
                headers={"Authorization": f"Bearer {gateway_token}"},
                json={"block_type": "work_batch", "status": "pending", "data": batch},
            )
            assert created.status_code in {200, 201}, "Gateway rejected the recovery batch"
            delivery = GatewayDeliveryClient(gateway_base, worker_token, http)
            claim = await delivery.claim(batch_id)
            request = CodexRunRequest(
                project_id=project_id,
                run_id=run_id,
                trace_id=f"trace-{unique}",
                batch_id=batch_id,
                worker_id=worker_id,
                claim_id=claim.claim_id,
                fencing_token=claim.fencing_token,
                session_id=f"session-{unique}",
                claim_token=claim.token,
                iteration=claim.iteration,
                command=("codex", "exec", "--json", "write no files"),
                workspace=tmp_path,
                project_root=tmp_path,
            )
            first_repository = GatewayCodexRunRepository(
                client=delivery,
                project_id=project_id,
                run_id=run_id,
                actor=worker_id,
                now=lambda: datetime.now(timezone.utc),
            )
            started = await first_repository.start(request)
            assert started.session_id == request.session_id
            before_restart = await delivery.delivery_events(
                project_id=project_id,
                run_id=run_id,
            )
            assert [event.event_type for event in before_restart] == [
                "codex_session_started"
            ]

            process.terminate()
            process.wait(timeout=15)
            process = _start_gateway(
                dsn=dsn,
                token=gateway_token,
                worker_token=worker_token,
                port=gateway_port,
            )
            await _wait_for_gateway(gateway_base)

            restarted_delivery = GatewayDeliveryClient(gateway_base, worker_token, http)
            restarted_repository = GatewayCodexRunRepository(
                client=restarted_delivery,
                project_id=project_id,
                run_id=run_id,
                actor=worker_id,
                now=lambda: datetime.now(timezone.utc),
            )
            runner = _RunnerThatMustNotStart()
            recovered = await CodexSupervisor(
                runner=runner,
                gateway=_UnusedLegacyEvidenceWriter(),
                policy=_RestartPolicy(),
                repository=restarted_repository,
            ).run(request)
            assert recovered.status is PackageExecutionStatus.EVIDENCE_UNRESOLVED
            assert runner.calls == 0
            after_restart = await restarted_delivery.delivery_events(
                project_id=project_id,
                run_id=run_id,
            )
            assert after_restart == before_restart
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=15)
