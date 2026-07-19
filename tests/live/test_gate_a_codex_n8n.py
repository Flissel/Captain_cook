from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from uuid import uuid4

import httpx
import pytest

from agenten.agent_runtime.n8n_endpoint import resolve_n8n_endpoint
from agenten.delivery.codex_runs import GatewayCodexRunRepository
from agenten.delivery.gateway_client import GatewayDeliveryClient
from agenten.execution.codex_policy import CodexExecutionPolicy
from agenten.execution.codex_supervisor import (
    CodexRunRequest,
    CodexSupervisor,
    PowerShellCodexRunner,
)
from agenten.execution.process import PackageExecutionStatus
from agenten.targets.n8n import N8nHttpClient, N8nTarget, SealedArtifact, ValidationCase
from gateway.contracts import DeliveryEventEnvelope


def _secret(name: str) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    for env_path in (
        Path(__file__).resolve().parents[2] / ".env.captain-n8n",
        Path(__file__).resolve().parents[2] / ".env",
        Path(__file__).resolve().parents[3] / ".env",
    ):
        if not env_path.exists():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            key, separator, candidate = line.partition("=")
            if separator and key.strip() == name and candidate.strip():
                return candidate.strip().strip('"').strip("'")
    return None


def _required(name: str, reason: str) -> str:
    value = _secret(name)
    if value is None:
        pytest.skip(f"Gate A prerequisite missing: {reason}")
    return value


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=workspace,
        check=True,
        capture_output=True,
        text=True,
    )


def _codex_binary() -> Path | None:
    relative_binary = Path(
        "@openai/codex/node_modules/@openai/codex-win32-x64/"
        "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
    )
    roots = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        roots.append(Path(appdata) / "npm" / "node_modules")
    try:
        npm = subprocess.run(
            ["npm", "root", "-g"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass
    else:
        roots.append(Path(npm))

    for root in roots:
        candidate = root / relative_binary
        if candidate.is_file():
            return candidate
    return None


class _ForbiddenLegacyWriter:
    def __getattr__(self, name: str) -> object:
        raise AssertionError(f"legacy evidence writer used: {name}")


@pytest.mark.live
@pytest.mark.asyncio
async def test_gate_a_real_codex_n8n_gateway_trace(
    record_property: Callable[[str, object], None],
) -> None:
    captain_port = _required(
        "CAPTAIN_N8N_PORT",
        "CAPTAIN_N8N_PORT for isolated Captain builder",
    )
    endpoint = resolve_n8n_endpoint(
        {
            "N8N_MODE": "captain-builder",
            "CAPTAIN_N8N_URL": (
                _secret("CAPTAIN_N8N_URL")
                or f"http://127.0.0.1:{captain_port}"
            ),
            "CAPTAIN_N8N_API_KEY": _required(
                "CAPTAIN_N8N_API_KEY",
                "CAPTAIN_N8N_API_KEY for isolated Captain builder",
            ),
        }
    )
    ledger_dsn = _required(
        "TEST_MARIADB_DSN",
        "TEST_MARIADB_DSN for isolated Gateway/MariaDB evidence",
    )
    _required("OPENAI_API_KEY", "OPENAI_API_KEY for real Codex CLI")
    codex_binary = _codex_binary()
    if codex_binary is None:
        pytest.skip("Gate A prerequisite missing: official Codex CLI native binary")

    unique = uuid4().hex
    project_id = f"gate-a-{unique[:12]}"
    run_id = f"run-{unique[:20]}"
    trace_id = f"trace-{unique[:20]}"
    batch_id = f"gate-a-{unique[:20]}"
    worker_id = f"worker-{unique[:12]}"
    session_id = f"session-{unique[:20]}"
    claim_id: str
    artifact_id = f"workflow-{unique[:16]}"
    case_id = f"case-{unique[:16]}"
    correlation_id = f"corr-{unique[:20]}"
    captain_token = secrets.token_urlsafe(32)
    worker_token = secrets.token_urlsafe(32)
    gateway_port = _free_port()
    gateway_base = f"http://127.0.0.1:{gateway_port}"
    gateway_delivery_event_id = uuid4()

    gateway_env = os.environ.copy()
    gateway_env.update(
        {
            "LEDGER_DSN": ledger_dsn,
            "CAPTAIN_GATEWAY_TOKEN": captain_token,
            "WORKER_GATEWAY_TOKEN": worker_token,
            "GATEWAY_PORT": str(gateway_port),
        }
    )
    gateway_process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "gateway.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(gateway_port),
            "--log-level",
            "error",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=gateway_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deployment = None
    try:
        for _ in range(60):
            try:
                if httpx.get(f"{gateway_base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        else:
            pytest.fail("ephemeral HTTP Gateway did not become ready")

        captain_headers = {"Authorization": f"Bearer {captain_token}"}
        worker_headers = {"Authorization": f"Bearer {worker_token}"}
        batch = {
            "batch_id": batch_id,
            "title": "Gate A harmless n8n workflow",
            "goal": "Create and validate one namespaced workflow",
            "subtask_ids": [f"subtask-{batch_id}"],
            "target": "n8n",
            "capability_tags": ["gate-a"],
            "depends_on": [],
            "constraints": [],
            "acceptance_criteria": [
                {
                    "assertion_id": "linked-execution",
                    "kind": "status_equals",
                    "expected": "succeeded",
                }
            ],
            "golden_cases": [],
        }
        with httpx.Client(base_url=gateway_base, timeout=10) as sync_http:
            created = sync_http.post(
                "/blocks",
                headers=captain_headers,
                json={"block_type": "work_batch", "status": "pending", "data": batch},
            )
            created.raise_for_status()
            claimed = sync_http.post(
                f"/batches/{batch_id}/claim",
                headers=worker_headers,
            )
            claimed.raise_for_status()
            claim = claimed.json()
        claim_id = claim["claim_id"]
        fencing_token = claim["fencing_token"]

        with tempfile.TemporaryDirectory(prefix="captain-gate-a-") as directory:
            temporary_root = Path(directory)
            workspace = temporary_root / "workspace"
            workspace.mkdir()
            codex_home = temporary_root / "codex-home"
            codex_home.mkdir()
            source_auth = Path.home() / ".codex" / "auth.json"
            if source_auth.is_file():
                shutil.copy2(source_auth, codex_home / "auth.json")
            _git(workspace, "init", "-q")
            _git(workspace, "config", "user.email", "gate-a@localhost")
            _git(workspace, "config", "user.name", "Captain Gate A")
            (workspace / "README.md").write_text("Gate A workspace\n", encoding="utf-8")
            _git(workspace, "add", "README.md")
            _git(workspace, "commit", "-qm", "chore: initialize gate workspace")

            workflow_path = workspace / "workflow.json"
            workflow_blueprint = {
                "nodes": [
                    {
                        "name": "Webhook",
                        "type": "n8n-nodes-base.webhook",
                        "typeVersion": 2,
                        "position": [0, 0],
                        "parameters": {
                            "httpMethod": "POST",
                            "path": "{{CAPTAIN_WEBHOOK_PATH}}",
                            "responseMode": "lastNode",
                            "options": {},
                        },
                    },
                    {
                        "name": "Code",
                        "type": "n8n-nodes-base.code",
                        "typeVersion": 2,
                        "position": [240, 0],
                        "parameters": {
                            "jsCode": "return [{ json: $input.first().json }];",
                        },
                    },
                ],
                "connections": {
                    "Webhook": {
                        "main": [[{"node": "Code", "type": "main", "index": 0}]]
                    }
                },
                "settings": {"executionOrder": "v1"},
            }
            prompt = (
                "Create exactly one file named workflow.json in the current workspace. "
                "Its complete UTF-8 content must be exactly the JSON below. Do not modify "
                "any other file, do not commit, and stop after writing it.\n\n"
                + json.dumps(workflow_blueprint, indent=2)
                + "\n"
            )
            request = CodexRunRequest(
                project_id=project_id,
                run_id=run_id,
                trace_id=trace_id,
                batch_id=batch_id,
                worker_id=worker_id,
                claim_id=claim_id,
                fencing_token=fencing_token,
                session_id=session_id,
                claim_token=claim["claim_token"],
                iteration=1,
                command=("codex", "exec", "--json", prompt),
                workspace=workspace,
                project_root=workspace,
            )
            policy = CodexExecutionPolicy(
                workspace_root=workspace,
                environment=os.environ,
            )
            async with httpx.AsyncClient(timeout=30) as gateway_http:
                gateway_client = GatewayDeliveryClient(
                    gateway_base,
                    worker_token,
                    gateway_http,
                )
                repository = GatewayCodexRunRepository(
                    client=gateway_client,
                    project_id=project_id,
                    run_id=run_id,
                    actor=worker_id,
                    now=lambda: datetime.now(timezone.utc),
                )
                runner = PowerShellCodexRunner(
                    pwsh_path=Path(
                        r"C:\Program Files\PowerShell\7\pwsh.exe"
                    ),
                    script_path=Path("scripts/codex-session.ps1").resolve(),
                    codex_path=codex_binary,
                    session_id=session_id,
                    state_path=workspace / "codex-process.json",
                    artifact_references=(f"artifact://sealed/{artifact_id}",),
                    codex_home=codex_home,
                    timeout_seconds=300,
                )
                result = await CodexSupervisor(
                    runner=runner,
                    gateway=_ForbiddenLegacyWriter(),
                    policy=policy,
                    repository=repository,
                ).run(request)
                assert result.status is PackageExecutionStatus.SUCCEEDED
                codex_events = await gateway_client.delivery_events(
                    project_id=project_id,
                    run_id=run_id,
                )
                codex_item_types = sorted(
                    {
                        event.payload.item_type
                        for event in codex_events
                        if event.event_type == "codex_session_event"
                        and isinstance(event.payload.item_type, str)
                    }
                )
                assert workflow_path.is_file(), (
                    "Codex completed without the required workspace artifact; "
                    f"recorded item types: {codex_item_types}"
                )

                sealed_bytes = workflow_path.read_bytes()
                artifact_digest = hashlib.sha256(sealed_bytes).hexdigest()
                workflow = json.loads(sealed_bytes)
                artifact = SealedArtifact(
                    artifact_id=artifact_id,
                    artifact_digest=artifact_digest,
                    namespace=project_id,
                    workflow=workflow,
                )

                async with httpx.AsyncClient(timeout=30) as n8n_http:
                    target = N8nTarget(
                        N8nHttpClient.from_endpoint(endpoint, n8n_http)
                    )
                    deployment = await target.deploy(artifact)
                    execution = await target.execute(
                        deployment,
                        ValidationCase(
                            case_id=case_id,
                            correlation_id=correlation_id,
                            input_payload={"operation": "ping"},
                        ),
                    )

                trace = {
                    "project_id": project_id,
                    "run_id": run_id,
                    "trace_id": trace_id,
                    "batch_id": batch_id,
                    "worker_id": worker_id,
                    "claim_id": claim_id,
                    "fencing_token": fencing_token,
                    "session_id": session_id,
                    "artifact_id": artifact_id,
                    "case_id": case_id,
                }
                now = datetime.now(timezone.utc)
                raw_events = [
                    {
                        "event_id": uuid4(),
                        "event_type": "artifact_built",
                        "occurred_at": now,
                        "actor": worker_id,
                        "trace": trace,
                        "payload": {
                            "event_type": "artifact_built",
                            "artifact_id": artifact_id,
                            "artifact_version": "1",
                            "sha256": artifact_digest,
                            "artifact_type": "n8n-workflow",
                            "sealed_ref": f"artifact://sealed/{artifact_id}",
                        },
                    },
                    {
                        "event_id": uuid4(),
                        "event_type": "deploy",
                        "occurred_at": now,
                        "actor": worker_id,
                        "trace": trace,
                        "payload": {
                            "event_type": "deploy",
                            "deployment_id": deployment.workflow_id,
                            "target": "n8n",
                            "artifact_version": "1",
                            "external_deployment_ref": (
                                f"artifact://n8n/workflows/{deployment.workflow_id}"
                            ),
                            "result": "succeeded",
                        },
                    },
                    {
                        "event_id": uuid4(),
                        "event_type": "validation_run",
                        "occurred_at": now,
                        "actor": worker_id,
                        "trace": trace,
                        "payload": {
                            "event_type": "validation_run",
                            "validation_id": execution.execution_id,
                            "layer": "live",
                            "case_ids": [case_id],
                            "assertion_results": {
                                "linked-execution": "passed"
                            },
                            "evidence_refs": [
                                f"artifact://n8n/executions/{execution.execution_id}",
                                f"artifact://sealed/{artifact_digest}",
                            ],
                            "artifact_version": "1",
                            "passed": True,
                        },
                    },
                    {
                        "event_id": gateway_delivery_event_id,
                        "event_type": "e2e_run",
                        "occurred_at": now,
                        "actor": worker_id,
                        "trace": trace,
                        "payload": {
                            "event_type": "e2e_run",
                            "e2e_run_id": f"e2e-{unique[:16]}",
                            "run_index": 1,
                            "clean": True,
                            "trace_complete": True,
                            "evidence_refs": [
                                f"artifact://n8n/workflows/{deployment.workflow_id}",
                                f"artifact://n8n/executions/{execution.execution_id}",
                                f"artifact://codex/sessions/{session_id}",
                                f"artifact://sealed/{artifact_digest}",
                            ],
                        },
                    },
                ]
                for raw in raw_events:
                    await gateway_client.append_delivery_event(
                        DeliveryEventEnvelope.model_validate(raw)
                    )
                events = await gateway_client.delivery_events(
                    project_id=project_id,
                    run_id=run_id,
                )

            serialized = json.dumps(
                [event.model_dump(mode="json") for event in events],
                sort_keys=True,
            )
            assert deployment.workflow_id in serialized
            assert execution.execution_id in serialized
            assert session_id in serialized
            assert artifact_digest in serialized
            assert run_id in serialized
            assert str(gateway_delivery_event_id) in serialized
            assert endpoint.mode == "captain-builder"
            assert endpoint.api_base_url in {
                _secret("CAPTAIN_N8N_URL"),
                f"http://127.0.0.1:{captain_port}",
            }
            record_property(
                "n8n_target_identity",
                f"{endpoint.mode}:{endpoint.api_base_url}",
            )
            record_property("workflow_id", deployment.workflow_id)
            record_property("execution_id", execution.execution_id)
            record_property("artifact_digest", artifact_digest)
            record_property("correlation_id", correlation_id)
            record_property(
                "gateway_delivery_event_id",
                str(gateway_delivery_event_id),
            )
            assert {
                event.event_type for event in events
            }.issuperset(
                {
                    "codex_session_started",
                    "codex_session_finished",
                    "artifact_built",
                    "deploy",
                    "validation_run",
                    "e2e_run",
                }
            )
    finally:
        primary_error = sys.exception()
        cleanup_errors: list[str] = []
        if deployment is not None:
            with httpx.Client(
                base_url=endpoint.api_base_url,
                headers={"X-N8N-API-KEY": endpoint.api_key},
                timeout=10,
            ) as cleanup:
                for operation, path in (
                    (
                        "deactivate",
                        f"/api/v1/workflows/{deployment.workflow_id}/deactivate",
                    ),
                    (
                        "delete",
                        f"/api/v1/workflows/{deployment.workflow_id}",
                    ),
                ):
                    try:
                        response = (
                            cleanup.post(path)
                            if operation == "deactivate"
                            else cleanup.delete(path)
                        )
                        response.raise_for_status()
                    except httpx.HTTPError as exc:
                        cleanup_errors.append(
                            f"n8n workflow {operation} cleanup failed: "
                            f"{type(exc).__name__}"
                        )
        gateway_process.terminate()
        try:
            gateway_process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gateway_process.kill()
            gateway_process.wait(timeout=10)
        if cleanup_errors:
            message = "; ".join(cleanup_errors)
            if primary_error is not None:
                primary_error.add_note(message)
            else:
                pytest.fail(message)
